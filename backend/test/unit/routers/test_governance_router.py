from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.routers.governance_router as governance_router_module
from server.routers.governance_router import governance
from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.governance.duplicate_knowledge_service import DuplicateKnowledgeService
from yuxi.storage.postgres.models_business import Base, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuDuplicateRelationDecision,
    FeishuKnowledgeSourceFragment,
    FeishuLogicalKnowledge,
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
    FeishuSourceSegment,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeFile,
)

pytestmark = pytest.mark.asyncio


async def test_office_pdf_cache_survives_memory_cache_reset(tmp_path, monkeypatch):
    conversions = 0

    async def fake_convert(_filename, _content):
        nonlocal conversions
        conversions += 1
        return b"%PDF-cached-preview"

    monkeypatch.setattr(governance_router_module, "_PREVIEW_PDF_CACHE_DIR", tmp_path)
    monkeypatch.setattr(governance_router_module, "convert_office_to_pdf", fake_convert)
    governance_router_module._OFFICE_PDF_CACHE.clear()
    governance_router_module._OFFICE_PDF_LOCKS.clear()

    first = await governance_router_module._cached_office_pdf("version:hash", "source.pptx", b"pptx")
    governance_router_module._OFFICE_PDF_CACHE.clear()
    second = await governance_router_module._cached_office_pdf("version:hash", "source.pptx", b"pptx")

    assert first == second == b"%PDF-cached-preview"
    assert conversions == 1


async def test_office_pdf_cache_prunes_memory_and_disk_by_size(tmp_path, monkeypatch):
    memory_cache = governance_router_module.OrderedDict()
    governance_router_module._remember_bytes(
        memory_cache,
        "first",
        b"1234",
        limit=10,
        max_bytes=6,
    )
    governance_router_module._remember_bytes(
        memory_cache,
        "second",
        b"5678",
        limit=10,
        max_bytes=6,
    )

    assert list(memory_cache) == ["second"]

    monkeypatch.setattr(governance_router_module, "_PREVIEW_PDF_CACHE_DIR", tmp_path)
    monkeypatch.setattr(governance_router_module, "_PREVIEW_PDF_DISK_LIMIT", 10)
    monkeypatch.setattr(governance_router_module, "_PREVIEW_PDF_DISK_MAX_BYTES", 30)
    for cache_key in ("first", "second", "third"):
        governance_router_module._write_preview_pdf(cache_key, b"%PDF-1234567890")

    cached_files = list(tmp_path.glob("*.pdf"))
    assert len(cached_files) == 2
    assert sum(item.stat().st_size for item in cached_files) == 30


@pytest.fixture
async def governance_api_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        admin_a = User(username="管理员甲", uid="admin-a", password_hash="x", role="admin")
        admin_b = User(username="管理员乙", uid="admin-b", password_hash="x", role="admin")
        source = FeishuSource(
            source_id="source-1",
            name="SD 知识库",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_USER_OAUTH",
        )
        item = FeishuSourceItem(
            item_id="item-1",
            source_id="source-1",
            item_key="page:item-1",
            item_type="docx",
            title="部署指南",
            source_validity="valid",
        )
        version = FeishuMaterialVersion(
            version_id="version-1",
            item_id="item-1",
            revision="1",
            content_hash="hash-1",
            processing_status="awaiting_review",
            processing_params={"content_quality": {"checked": True, "has_body": True}},
            review_status="pending",
            yuxi_file_id="file-1",
        )
        session.add_all([admin_a, admin_b, source, item, version])
        await session.commit()

        current_user = {"value": admin_a}
        app = FastAPI()
        app.include_router(governance, prefix="/api")

        async def admin_override():
            return current_user["value"]

        async def db_override():
            yield session

        app.dependency_overrides[get_admin_user] = admin_override
        app.dependency_overrides[get_db] = db_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, session, current_user, admin_b
    await engine.dispose()


async def add_duplicate_relation_data(
    session,
    *,
    suffix: str = "duplicate",
    relation_type: str = "EXACT_DUPLICATE",
    matching_chunks: bool = True,
    create_chunks: bool = True,
):
    target_item_id = f"item-{suffix}"
    target_version_id = f"version-{suffix}"
    target_file_id = f"file-{suffix}"
    relation_id = f"relation-{suffix}"
    session.add_all(
        [
            KnowledgeBase(kb_id="kb-1", name="SD 知识库", kb_type="normal"),
            KnowledgeFile(file_id="file-1", kb_id="kb-1", filename="部署指南.docx"),
            KnowledgeFile(file_id=target_file_id, kb_id="kb-1", filename="产品介绍.pptx"),
            FeishuSourceItem(
                item_id=target_item_id,
                source_id="source-1",
                item_key=f"page:{target_item_id}",
                item_type="pptx",
                title="产品介绍",
                path_text="产品资料 / 产品介绍",
                source_validity="valid",
            ),
            FeishuMaterialVersion(
                version_id=target_version_id,
                item_id=target_item_id,
                revision="1",
                content_hash=f"hash-{suffix}",
                processing_status="awaiting_review",
                review_status="pending",
                yuxi_file_id=target_file_id,
            ),
            FeishuCrossDocumentRelation(
                relation_id=relation_id,
                comparison_key=f"version-1:{target_version_id}",
                source_version_id="version-1",
                target_version_id=target_version_id,
                relation_type=relation_type,
                similarity=0.98,
                confidence=0.96,
                same_content=["公司简介一致"],
                different_content=[],
                scope_difference={},
                reasoning="两份资料包含相同的公司简介",
                status="open",
            ),
        ]
    )
    source_content = (
        "公司简介\n狗狗你是公司专注企业数字化服务，为客户提供知识管理、智能助手、"
        "项目实施和持续运营服务，帮助企业沉淀并安全使用正式知识资产。"
    )
    target_content = (
        source_content if matching_chunks else "产品参数\n本产品采用独立部署方式，主要用于项目实施和现场交付管理。"
    )
    if create_chunks:
        session.add_all(
            [
                KnowledgeChunk(
                    chunk_id=f"chunk-source-{suffix}",
                    file_id="file-1",
                    kb_id="kb-1",
                    chunk_index=0,
                    content=source_content,
                ),
                KnowledgeChunk(
                    chunk_id=f"chunk-target-{suffix}",
                    file_id=target_file_id,
                    kb_id="kb-1",
                    chunk_index=0,
                    content=target_content,
                ),
            ]
        )
    await session.commit()
    return relation_id


async def test_governance_review_transfer_blocks_old_reviewer_and_allows_new_assignee(governance_api_fixture):
    client, _, current_user, admin_b = governance_api_fixture

    listed = await client.get("/api/governance/reviews", params={"source_id": "source-1"})
    review_id = listed.json()["items"][0]["review_id"]
    transferred = await client.post(
        f"/api/governance/reviews/{review_id}/resolve",
        json={
            "decision": "TRANSFER",
            "action": "KEEP_CURRENT",
            "problem_tags": [],
            "decision_comment": "请产品负责人确认",
            "applicability_scope": {},
            "assignee_id": "admin-b",
        },
    )

    assert listed.status_code == 200
    assert transferred.status_code == 200
    assert transferred.json()["assignee_id"] == "admin-b"
    forbidden = await client.post(
        f"/api/governance/reviews/{transferred.json()['review_id']}/resolve",
        json={
            "decision": "REQUEST_CHANGES",
            "action": "MARK_INSUFFICIENT",
            "problem_tags": ["MISSING_SCOPE"],
            "decision_comment": "请补充版本",
            "applicability_scope": {},
        },
    )
    assert forbidden.status_code == 403

    current_user["value"] = admin_b
    resolved = await client.post(
        f"/api/governance/reviews/{transferred.json()['review_id']}/resolve",
        json={
            "decision": "REQUEST_CHANGES",
            "action": "MARK_INSUFFICIENT",
            "problem_tags": ["MISSING_SCOPE"],
            "decision_comment": "请补充版本",
            "applicability_scope": {"product": "Q900"},
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "changes_requested"


async def test_governance_reviewer_list_uses_active_admin_users(governance_api_fixture):
    client, _, _, _ = governance_api_fixture

    response = await client.get("/api/governance/reviewers")

    assert response.status_code == 200
    assert sorted(response.json()["items"], key=lambda item: item["user_id"]) == [
        {"user_id": "admin-a", "name": "管理员甲", "role": "admin"},
        {"user_id": "admin-b", "name": "管理员乙", "role": "admin"},
    ]


async def test_governance_blocks_publish_when_conflict_is_unresolved(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    session.add(
        FeishuCrossDocumentRelation(
            relation_id="relation-conflict",
            comparison_key="version-1:version-1",
            source_version_id="version-1",
            target_version_id="version-1",
            relation_type="CONFLICT",
            similarity=0.8,
            confidence=0.9,
            same_content=[],
            different_content=[{"field": "部署模式", "current": "A", "candidate": "B"}],
            scope_difference={},
            reasoning="相同条件下结论不同",
            status="open",
        )
    )
    await session.commit()

    listed = await client.get("/api/governance/reviews", params={"source_id": "source-1"})
    review_id = listed.json()["items"][0]["review_id"]
    response = await client.post(
        f"/api/governance/reviews/{review_id}/resolve",
        json={
            "decision": "PUBLISH",
            "action": "CREATE",
            "problem_tags": ["CONFLICT"],
            "decision_comment": "直接创建新知识",
            "applicability_scope": {},
        },
    )

    assert response.status_code == 409
    assert "未解决" in response.json()["detail"]


async def test_review_package_list_backfills_pending_material_and_returns_real_counts(governance_api_fixture):
    client, _, _, _ = governance_api_fixture

    response = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["counts"]["mine"] == 1
    assert body["items"][0]["item_count"] == 1
    assert body["items"][0]["source_version_id"] == "version-1"
    assert body["items"][0]["review_type_counts"] == {"NEW": 1}

    detail = await client.get(f"/api/governance/review-packages/{body['items'][0]['package_id']}")
    assert detail.status_code == 200
    assert detail.json()["items"][0]["allowed_outcomes"] == [
        "PUBLISH",
        "REQUEST_SOURCE_CHANGE",
        "EXCLUDE",
    ]


async def test_review_package_segments_return_stable_locator_and_publication_state(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    session.add(
        FeishuSourceSegment(
            segment_id="seg-1",
            segment_key="stable-section-key",
            version_id="version-1",
            item_id="item-1",
            yuxi_file_id="file-1",
            segment_index=0,
            segment_type="table",
            title_path=["部署要求", "资源配置"],
            locator_json={"page": 3, "block": 1},
            content="| 资源 | 最低配置 |\n| --- | --- |\n| CPU | 8 核 |",
            content_hash="segment-hash",
            token_count=24,
            publication_state="PENDING",
            status="ACTIVE",
        )
    )
    await session.commit()
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]

    response = await client.get(f"/api/governance/review-packages/{package_id}/segments")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["token_count"] == 24
    assert response.json()["items"][0]["segment_id"] == "seg-1"
    assert response.json()["items"][0]["locator_label"] == "第3页"
    assert response.json()["items"][0]["title_path"] == ["部署要求", "资源配置"]
    assert response.json()["items"][0]["publication_state"] == "PENDING"


async def test_review_package_update_returns_previous_published_version(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    source_item = await session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-1"))
    source_item.active_version_id = "version-0"
    session.add(
        FeishuMaterialVersion(
            version_id="version-0",
            item_id="item-1",
            revision="0",
            content_hash="hash-0",
            processing_status="published",
            review_status="approved",
            yuxi_file_id="file-0",
            chunk_count=3,
            token_count=80,
        )
    )
    await session.commit()

    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = await client.get(f"/api/governance/review-packages/{package_id}")

    assert detail.status_code == 200
    assert detail.json()["items"][0]["review_type"] == "UPDATE"
    assert detail.json()["previous_version"] == {
        "version_id": "version-0",
        "revision": "0",
        "yuxi_file_id": "file-0",
        "chunk_count": 3,
        "token_count": 80,
        "published_at": None,
    }


async def test_review_package_update_falls_back_to_latest_prior_published_version(
    governance_api_fixture,
):
    client, session, _, _ = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    package_item = await session.scalar(select(FeishuReviewItem).where(FeishuReviewItem.package_id == package_id))
    source_item = await session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-1"))
    current_version = await session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1")
    )
    package_item.review_type = "UPDATE"
    package_item.evidence_json = {}
    source_item.active_version_id = "version-1"
    current_version.processing_status = "published"
    current_version.review_status = "approved"
    current_version.yuxi_file_id = "file-1"
    current_version.published_at = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
    session.add(
        FeishuMaterialVersion(
            version_id="version-0",
            item_id="item-1",
            revision="0",
            content_hash="hash-0",
            processing_status="replaced",
            review_status="approved",
            yuxi_file_id="file-0",
            chunk_count=3,
            token_count=80,
            published_at=datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
        )
    )
    await session.commit()

    detail = await client.get(f"/api/governance/review-packages/{package_id}")

    assert detail.status_code == 200
    assert detail.json()["previous_version"]["version_id"] == "version-0"
    assert detail.json()["previous_version"]["yuxi_file_id"] == "file-0"


async def test_review_package_draft_persists_and_rejects_stale_lock(governance_api_fixture):
    client, _, _, _ = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = await client.get(f"/api/governance/review-packages/{package_id}")

    saved = await client.patch(
        f"/api/governance/review-packages/{package_id}/draft",
        json={
            "lock_version": detail.json()["lock_version"],
            "draft": {"decision_comment": "请补充适用版本"},
        },
    )
    refreshed = await client.get(f"/api/governance/review-packages/{package_id}")
    stale = await client.patch(
        f"/api/governance/review-packages/{package_id}/draft",
        json={"lock_version": detail.json()["lock_version"], "draft": {"decision_comment": "覆盖草稿"}},
    )

    assert saved.status_code == 200
    assert saved.json()["lock_version"] == detail.json()["lock_version"] + 1
    assert refreshed.json()["draft"] == {"decision_comment": "请补充适用版本"}
    assert stale.status_code == 409
    assert "lock_version" in stale.json()["detail"]


async def test_review_package_transfer_changes_editor_without_terminal_status(governance_api_fixture):
    client, _, current_user, admin_b = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = await client.get(f"/api/governance/review-packages/{package_id}")

    transferred = await client.post(
        f"/api/governance/review-packages/{package_id}/transfer",
        json={
            "lock_version": detail.json()["lock_version"],
            "assignee_id": "admin-b",
            "comment": "转交产品负责人",
        },
    )
    forbidden = await client.patch(
        f"/api/governance/review-packages/{package_id}/draft",
        json={"lock_version": transferred.json()["lock_version"], "draft": {"note": "old owner"}},
    )
    old_owner_mine = await client.get(
        "/api/governance/review-packages", params={"source_id": "source-1", "view": "mine"}
    )
    transferred_history = await client.get(
        "/api/governance/review-packages",
        params={"source_id": "source-1", "view": "transferred_by_me"},
    )
    current_user["value"] = admin_b
    new_owner_mine = await client.get(
        "/api/governance/review-packages", params={"source_id": "source-1", "view": "mine"}
    )
    allowed = await client.patch(
        f"/api/governance/review-packages/{package_id}/draft",
        json={"lock_version": transferred.json()["lock_version"], "draft": {"note": "new owner"}},
    )

    assert transferred.status_code == 200
    assert transferred.json()["assignee_id"] == "admin-b"
    assert forbidden.status_code == 403
    assert old_owner_mine.json()["total"] == 0
    assert transferred_history.json()["total"] == 1
    assert new_owner_mine.json()["total"] == 1
    assert allowed.status_code == 200
    assert (await client.get(f"/api/governance/review-packages/{package_id}")).json()["workflow_status"] == "OPEN"


async def test_review_package_request_source_change_is_persisted_and_idempotent(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()
    review_item_id = detail["items"][0]["review_item_id"]
    request = {
        "request_id": "request-source-change-1",
        "lock_version": detail["lock_version"],
        "decisions": [
            {
                "review_item_id": review_item_id,
                "outcome": "REQUEST_SOURCE_CHANGE",
                "problem_tags": ["MISSING_SCOPE"],
                "decision_comment": "请在飞书原文补充产品版本",
                "applicability_scope": {"product": "Q900"},
                "responsible_user_name": "资料负责人",
            }
        ],
    }

    resolved = await client.post(f"/api/governance/review-packages/{package_id}/resolve", json=request)
    replayed = await client.post(f"/api/governance/review-packages/{package_id}/resolve", json=request)
    refreshed = await client.get(f"/api/governance/review-packages/{package_id}")

    assert resolved.status_code == 200
    assert resolved.json()["workflow_status"] == "WAITING_SOURCE_CHANGE"
    assert replayed.status_code == 200
    assert replayed.json()["idempotent_replay"] is True
    assert len(refreshed.json()["change_requests"]) == 1
    assert refreshed.json()["change_requests"][0]["request_text"] == "请在飞书原文补充产品版本"
    assert await session.scalar(select(func.count()).select_from(FeishuSourceChangeRequest)) == 1


async def test_source_change_request_can_be_listed_opened_and_cancelled(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()
    await client.post(
        f"/api/governance/review-packages/{package_id}/resolve",
        json={
            "request_id": "request-to-cancel",
            "lock_version": detail["lock_version"],
            "decisions": [
                {
                    "review_item_id": detail["items"][0]["review_item_id"],
                    "outcome": "REQUEST_SOURCE_CHANGE",
                    "problem_tags": ["MISSING_SCOPE"],
                    "decision_comment": "请补充产品版本",
                    "applicability_scope": {},
                    "responsible_user_id": "owner-a",
                }
            ],
        },
    )

    requests = await client.get(
        "/api/governance/source-change-requests",
        params={"source_id": "source-1", "status": "OPEN", "responsible_user_id": "owner-a"},
    )
    change_request_id = requests.json()["items"][0]["change_request_id"]
    opened = await client.get(f"/api/governance/source-change-requests/{change_request_id}")
    cancelled = await client.post(
        f"/api/governance/source-change-requests/{change_request_id}/cancel",
        json={"reason": "资料负责人确认无需修改"},
    )
    cancelled_again = await client.post(
        f"/api/governance/source-change-requests/{change_request_id}/cancel",
        json={"reason": "重复取消"},
    )
    event = await session.scalar(
        select(FeishuProcessingEvent).where(FeishuProcessingEvent.event_type == "source_change_request_cancelled")
    )

    assert requests.status_code == 200
    assert requests.json()["total"] == 1
    assert opened.status_code == 200
    assert opened.json()["request_text"] == "请补充产品版本"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["review_item_status"] == "INVALIDATED"
    assert cancelled.json()["workflow_status"] == "COMPLETED"
    assert cancelled_again.status_code == 409
    assert event.message == "资料负责人确认无需修改"


async def test_review_package_publish_uses_existing_material_publish_chain(governance_api_fixture, monkeypatch):
    client, session, _, _ = governance_api_fixture

    class _Task:
        id = "publish-task-1"

    async def fake_enqueue_publish(version_id: str, *, operator_id: str):
        assert version_id == "version-1"
        assert operator_id == "admin-a"
        return _Task()

    monkeypatch.setattr(governance_router_module, "_enqueue_publish", fake_enqueue_publish)
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()

    response = await client.post(
        f"/api/governance/review-packages/{package_id}/resolve",
        json={
            "request_id": "publish-request-1",
            "lock_version": detail["lock_version"],
            "decisions": [
                {
                    "review_item_id": detail["items"][0]["review_item_id"],
                    "outcome": "PUBLISH",
                    "problem_tags": [],
                    "applicability_scope": {},
                }
            ],
        },
    )
    version = await session.scalar(select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1"))

    assert response.status_code == 200
    assert response.json()["workflow_status"] == "COMPLETED"
    assert response.json()["publish_task_ids"] == ["publish-task-1"]
    assert version.review_status == "approved"
    assert version.processing_status == "publish_queued"


async def test_review_package_publishes_one_unit_without_consuming_the_rest(
    governance_api_fixture,
    monkeypatch,
):
    client, session, _, _ = governance_api_fixture
    session.add_all(
        [
            FeishuSourceSegment(
                segment_id="segment-unit-1",
                segment_key="unit-1",
                version_id="version-1",
                item_id="item-1",
                yuxi_file_id="file-1",
                segment_index=0,
                segment_type="paragraph",
                title_path=["安装步骤"],
                locator_json={"page": 1},
                content="安装前准备管理员账号和服务地址，然后按照向导完成部署。",
                content_hash="segment-unit-1-hash",
                publication_state="PENDING",
                status="ACTIVE",
            ),
            FeishuSourceSegment(
                segment_id="segment-unit-2",
                segment_key="unit-2",
                version_id="version-1",
                item_id="item-1",
                yuxi_file_id="file-1",
                segment_index=1,
                segment_type="paragraph",
                title_path=["验收检查"],
                locator_json={"page": 2},
                content="部署完成后检查登录、检索和权限隔离是否符合验收要求。",
                content_hash="segment-unit-2-hash",
                publication_state="PENDING",
                status="ACTIVE",
            ),
        ]
    )
    await session.commit()

    class _Task:
        id = "unit-publish-task-1"

    async def fake_enqueue_publish(version_id: str, *, operator_id: str):
        assert version_id == "version-1"
        assert operator_id == "admin-a"
        return _Task()

    monkeypatch.setattr(governance_router_module, "_enqueue_publish", fake_enqueue_publish)
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()

    response = await client.post(
        f"/api/governance/review-packages/{package_id}/resolve",
        json={
            "request_id": "publish-unit-request-1",
            "lock_version": detail["lock_version"],
            "decisions": [
                {
                    "review_item_id": detail["items"][0]["review_item_id"],
                    "outcome": "PUBLISH",
                    "problem_tags": [],
                    "applicability_scope": {},
                }
            ],
        },
    )
    segment_states = list(
        await session.scalars(select(FeishuSourceSegment.publication_state).order_by(FeishuSourceSegment.segment_index))
    )
    version = await session.scalar(select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1"))

    assert response.status_code == 200
    assert response.json()["workflow_status"] == "OPEN"
    assert response.json()["unit_publish_version_ids"] == ["version-1"]
    assert response.json()["publish_task_ids"] == ["unit-publish-task-1"]
    assert response.json()["remaining_unit_count"] == 1
    assert response.json()["included_unit_count"] == 1
    assert segment_states == ["INCLUDED", "PENDING"]
    assert version.review_status == "approved"
    assert version.processing_status == "publish_queued"


async def test_review_package_exclude_allows_empty_decision_comment(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()

    response = await client.post(
        f"/api/governance/review-packages/{package_id}/resolve",
        json={
            "request_id": "exclude-without-comment-1",
            "lock_version": detail["lock_version"],
            "decisions": [
                {
                    "review_item_id": detail["items"][0]["review_item_id"],
                    "outcome": "EXCLUDE",
                    "problem_tags": [],
                    "applicability_scope": {},
                }
            ],
        },
    )
    version = await session.scalar(select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1"))

    assert response.status_code == 200
    assert response.json()["workflow_status"] == "COMPLETED"
    assert response.json()["reject_candidates"] == [{"version_id": "version-1", "reason": "不纳入知识库"}]
    assert version.review_status == "rejected"


async def test_layout_endpoint_returns_pages_and_persists_review_draft(governance_api_fixture, monkeypatch):
    client, session, _, _ = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]

    async def fake_document_source(_package_id, _db):
        return (
            {
                "package_id": package_id,
                "source_version_id": "version-1",
                "source_content_hash": "hash-1",
                "draft": {},
            },
            "部署指南.pdf",
            b"pdf",
        )

    async def fake_build_layout(_filename, _content, *, segments=()):
        assert segments == []
        return {
            "supported": True,
            "filename": "部署指南.pdf",
            "file_type": ".pdf",
            "editable": True,
            "page_count": 1,
            "pages": [{"page_number": 1, "blocks": [{"block_id": "block-1", "content": "旧内容"}]}],
        }

    async def fake_page(_filename, _content, *, page_number, pdf_content=None):
        assert page_number == 1
        return b"image", "image/png"

    monkeypatch.setattr(governance_router_module, "_document_source", fake_document_source)
    monkeypatch.setattr(governance_router_module, "build_document_layout", fake_build_layout)
    monkeypatch.setattr(governance_router_module, "render_document_page", fake_page)

    layout = await client.get(f"/api/governance/review-packages/{package_id}/layout")
    page = await client.get(f"/api/governance/review-packages/{package_id}/layout/pages/1")
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()
    edited = await client.patch(
        f"/api/governance/review-packages/{package_id}/layout/edits",
        json={
            "lock_version": detail["lock_version"],
            "block_id": "block-1",
            "page_number": 1,
            "content": "新内容",
            "source_segment_ids": [],
        },
    )

    assert layout.status_code == 200
    assert layout.json()["pages"][0]["blocks"][0]["block_id"] == "block-1"
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert edited.status_code == 200
    assert edited.json()["draft"]["layout_edits"]["block-1"]["content"] == "新内容"


async def test_review_package_conflict_decision_resolves_only_linked_relation(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    relations = [
        FeishuCrossDocumentRelation(
            relation_id=f"relation-{index}",
            comparison_key=f"version-1:version-1:{index}",
            source_version_id="version-1",
            target_version_id="version-1",
            relation_type="CONFLICT",
            status="open",
        )
        for index in (1, 2)
    ]
    session.add_all(relations)
    await session.commit()
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    review_item = await session.scalar(select(FeishuReviewItem).where(FeishuReviewItem.package_id == package_id))
    review_item.relation_ids = ["relation-1"]
    await session.commit()
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()

    response = await client.post(
        f"/api/governance/review-packages/{package_id}/resolve",
        json={
            "request_id": "conflict-request-1",
            "lock_version": detail["lock_version"],
            "decisions": [
                {
                    "review_item_id": review_item.review_item_id,
                    "outcome": "KEEP_CURRENT",
                    "problem_tags": ["CONFLICT"],
                    "decision_comment": "保留当前正式版本",
                    "applicability_scope": {},
                }
            ],
        },
    )
    relation_one = await session.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == "relation-1")
    )
    relation_two = await session.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == "relation-2")
    )

    assert response.status_code == 200
    assert relation_one.status == "resolved"
    assert relation_two.status == "open"
    assert await session.scalar(select(func.count()).select_from(FeishuReviewPackage)) == 1


async def test_open_review_package_absorbs_conflict_evidence_created_after_initial_list(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    first = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = first.json()["items"][0]["package_id"]
    session.add(
        FeishuCrossDocumentRelation(
            relation_id="relation-late",
            comparison_key="version-1:version-1:late",
            source_version_id="version-1",
            target_version_id="version-1",
            relation_type="CONFLICT",
            status="open",
        )
    )
    await session.commit()

    second = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    detail = await client.get(f"/api/governance/review-packages/{package_id}")

    assert second.status_code == 200
    assert second.json()["total"] == 1
    assert second.json()["items"][0]["risk_level"] == "HIGH"
    assert detail.json()["items"][0]["review_type"] == "CONFLICT"
    assert detail.json()["items"][0]["relation_ids"] == ["relation-late"]


async def test_review_package_rejects_processing_failures_as_knowledge_problem_tags(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    listed = await client.get("/api/governance/review-packages", params={"source_id": "source-1"})
    package_id = listed.json()["items"][0]["package_id"]
    detail = (await client.get(f"/api/governance/review-packages/{package_id}")).json()

    response = await client.post(
        f"/api/governance/review-packages/{package_id}/resolve",
        json={
            "request_id": "invalid-processing-tag",
            "lock_version": detail["lock_version"],
            "decisions": [
                {
                    "review_item_id": detail["items"][0]["review_item_id"],
                    "outcome": "PUBLISH",
                    "problem_tags": ["PARSE_ERROR"],
                    "applicability_scope": {},
                }
            ],
        },
    )
    version = await session.scalar(select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1"))

    assert response.status_code == 409
    assert "Processing failures" in response.json()["detail"]
    assert version.review_status == "pending"


async def test_duplicate_candidates_return_matching_source_fragments(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(session)

    response = await client.get(f"/api/governance/relations/{relation_id}/duplicate-candidates")

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["title"] == "部署指南"
    assert body["target"]["title"] == "产品介绍"
    assert body["decision"] is None
    assert len(body["fragment_matches"]) == 1
    assert body["fragment_matches"][0]["similarity"] == 1.0
    assert "公司简介" in body["fragment_matches"][0]["source_excerpt"]
    assert "狗狗你是公司专注企业数字化服务" in body["fragment_matches"][0]["source_overlap_excerpt"]


async def test_relation_layout_comparison_returns_both_pages_and_match_blocks(
    governance_api_fixture,
    monkeypatch,
):
    client, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(session, suffix="layout")
    source_version = await session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1")
    )
    target_version = await session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-layout")
    )
    source_item = await session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-1"))
    target_item = await session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-layout")
    )

    async def fake_version_source(version_id, _db):
        if version_id == "version-1":
            return source_version, source_item, "source.docx", b"source"
        return target_version, target_item, "target.pptx", b"target"

    async def fake_build(filename, _content, *, segments=()):
        block_id = "source-block" if filename.startswith("source") else "target-block"
        unrelated_block_id = (
            "source-block-unrelated" if filename.startswith("source") else "target-block-unrelated"
        )
        return {
            "supported": True,
            "file_type": ".docx" if filename.startswith("source") else ".pptx",
            "page_count": 1,
            "pages": [
                {
                    "page_number": 1,
                    "aspect_ratio": 1.77,
                    "blocks": [
                        {
                            "block_id": block_id,
                            "content": "公司简介：狗狗你是公司专注企业数字化服务。",
                            "source_segment_ids": ["segment-shared"],
                        },
                        {
                            "block_id": unrelated_block_id,
                            "content": "1",
                            "source_segment_ids": ["segment-shared"],
                        },
                    ],
                }
            ],
        }

    class FakeDuplicateService:
        async def get_relation_candidates(self, _relation_id):
            return {
                "fragment_matches": [
                    {
                        "match_id": "layout-match",
                        "source_segment_id": "segment-shared",
                        "source_locator": {"page": 1},
                        "source_overlap_excerpt": "公司简介：狗狗你是公司专注企业数字化服务。",
                        "target_segment_id": "segment-shared",
                        "target_locator": {"page": 1},
                        "target_overlap_excerpt": "公司简介：狗狗你是公司专注企业数字化服务。",
                        "similarity": 1.0,
                    }
                ]
            }

    async def fake_duplicate_service(_db, _relation_id):
        return FakeDuplicateService()

    async def fake_render(_filename, _content, *, page_number, pdf_content=None):
        assert page_number == 1
        assert pdf_content == b"%PDF-cached"
        return b"png", "image/png"

    async def fake_cached_pdf(cache_key, filename, content):
        assert cache_key == "version-1:hash-1"
        assert filename == "source.docx"
        assert content == b"source"
        return b"%PDF-cached"

    monkeypatch.setattr(governance_router_module, "_version_source", fake_version_source)
    monkeypatch.setattr(governance_router_module, "build_document_layout", fake_build)
    monkeypatch.setattr(governance_router_module, "_duplicate_service", fake_duplicate_service)
    monkeypatch.setattr(governance_router_module, "_cached_office_pdf", fake_cached_pdf)
    monkeypatch.setattr(governance_router_module, "render_document_page", fake_render)
    governance_router_module._RELATION_LAYOUT_CACHE.clear()
    governance_router_module._RELATION_PAGE_CACHE.clear()

    response = await client.get(f"/api/governance/relations/{relation_id}/layout-comparison")
    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is True
    assert body["source"]["page_count"] == 1
    assert body["target"]["page_count"] == 1
    assert body["matches"][0]["source_block_ids"] == ["source-block"]
    assert body["matches"][0]["target_block_ids"] == ["target-block"]

    page = await client.get(f"/api/governance/relations/{relation_id}/layout-comparison/source/pages/1")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("image/png")
    assert page.content == b"png"


async def test_duplicate_candidates_fall_back_to_yuxi_parsed_content(governance_api_fixture):
    _, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(
        session,
        suffix="parsed-content",
        relation_type="OVERLAP",
        create_chunks=False,
    )
    shared = "善达信息专注企业数字化服务，为客户提供知识管理、智能助手和持续运营服务。"

    async def content_loader(file_id: str) -> str:
        if file_id == "file-1":
            return f"部署指南\n{shared}\n本资料还包含部署步骤。"
        return f"产品介绍\n{shared}\n本资料还包含产品参数。"

    response = await DuplicateKnowledgeService(
        session,
        content_loader=content_loader,
    ).get_relation_candidates(relation_id)

    assert len(response["fragment_matches"]) == 1
    assert response["fragment_matches"][0]["similarity"] == 1.0
    assert response["fragment_matches"][0]["source_overlap_excerpt"] == shared
    assert response["fragment_matches"][0]["target_overlap_excerpt"] == shared


async def test_duplicate_candidates_ignore_rendered_image_references(governance_api_fixture):
    _, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(
        session,
        suffix="parsed-images",
        relation_type="OVERLAP",
        create_chunks=False,
    )
    shared = "善达信息专注企业数字化服务，为客户提供知识管理、智能助手和持续运营服务。"

    async def content_loader(file_id: str) -> str:
        image_name = "source.png" if file_id == "file-1" else "target.png"
        return f"[{image_name}](/minio/public/kb/kb-images/{image_name})\n{shared}"

    response = await DuplicateKnowledgeService(
        session,
        content_loader=content_loader,
    ).get_relation_candidates(relation_id)

    assert len(response["fragment_matches"]) == 1
    assert response["fragment_matches"][0]["source_overlap_excerpt"] == shared
    assert "/kb-images/" not in response["fragment_matches"][0]["source_excerpt"]


async def test_duplicate_resolution_creates_one_logical_knowledge_and_is_idempotent(
    governance_api_fixture,
):
    client, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(session)
    payload = {"request_id": "duplicate-request-1", "strategy": "USE_SOURCE"}

    response = await client.post(
        f"/api/governance/relations/{relation_id}/resolve-duplicate",
        json=payload,
    )
    replay = await client.post(
        f"/api/governance/relations/{relation_id}/resolve-duplicate",
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["idempotent_replay"] is False
    assert response.json()["decision"]["primary_version_id"] == "version-1"
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    logical_knowledge = list(await session.scalars(select(FeishuLogicalKnowledge)))
    sources = list(
        await session.scalars(
            select(FeishuKnowledgeSourceFragment).order_by(FeishuKnowledgeSourceFragment.source_role.asc())
        )
    )
    relation = await session.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == relation_id)
    )
    assert len(logical_knowledge) == 1
    assert len(sources) == 2
    assert {source.source_role for source in sources} == {"PRIMARY", "ALIAS"}
    assert next(source for source in sources if source.source_role == "PRIMARY").version_id == "version-1"
    assert relation.status == "resolved"
    assert relation.human_decision == "MARK_DUPLICATE"
    assert await session.scalar(select(func.count()).select_from(FeishuDuplicateRelationDecision)) == 1


async def test_duplicate_resolution_rejects_second_decision_request(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(session)
    endpoint = f"/api/governance/relations/{relation_id}/resolve-duplicate"
    first = await client.post(endpoint, json={"request_id": "decision-1", "strategy": "USE_SOURCE"})

    second = await client.post(endpoint, json={"request_id": "decision-2", "strategy": "USE_TARGET"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already been resolved" in second.json()["detail"]


async def test_duplicate_resolution_can_keep_sources_separate(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(session, suffix="separate", relation_type="OVERLAP")

    response = await client.post(
        f"/api/governance/relations/{relation_id}/resolve-duplicate",
        json={"request_id": "separate-request", "strategy": "KEEP_SEPARATE"},
    )
    relation = await session.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == relation_id)
    )

    assert response.status_code == 200
    assert response.json()["decision"]["strategy"] == "KEEP_SEPARATE"
    assert await session.scalar(select(func.count()).select_from(FeishuLogicalKnowledge)) == 0
    assert relation.status == "resolved"
    assert relation.human_decision == "KEEP_SEPARATE"


async def test_duplicate_resolution_requires_supported_relation_and_matching_fragments(
    governance_api_fixture,
):
    client, session, _, _ = governance_api_fixture
    relation_id = await add_duplicate_relation_data(session, matching_chunks=False)

    no_match = await client.post(
        f"/api/governance/relations/{relation_id}/resolve-duplicate",
        json={"request_id": "no-match", "strategy": "USE_SOURCE"},
    )
    relation = await session.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == relation_id)
    )
    relation.relation_type = "CONFLICT"
    await session.commit()
    unsupported = await client.get(f"/api/governance/relations/{relation_id}/duplicate-candidates")

    assert no_match.status_code == 409
    assert "没有找到" in no_match.json()["detail"]
    assert unsupported.status_code == 409
    assert "不是可治理" in unsupported.json()["detail"]
