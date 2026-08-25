import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.source_segment_service import (
    SourceSegmentService,
    build_retrieval_chunks,
    build_source_segment_drafts,
    segment_ids_from_chunk_tags,
)
from yuxi.governance.duplicate_knowledge_service import DuplicateKnowledgeService
from yuxi.governance.schemas import DuplicateRelationResolutionRequest
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuKnowledgeSourceFragment,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)

pytestmark = pytest.mark.asyncio


def _drafts(content: str, filename: str = "guide.md", version_id: str = "version-1"):
    return build_source_segment_drafts(
        content,
        version_id=version_id,
        item_id="item-1",
        filename=filename,
    )


async def test_markdown_segments_keep_heading_path_and_stable_key():
    first = _drafts("# 安装指南\n\n## 准备工作\n\n请准备管理员账号、网络地址和授权信息。")
    changed = _drafts("# 安装指南\n\n## 准备工作\n\n请准备管理员账号、网络地址、授权信息和安装介质。")

    assert first[0].title_path == ("安装指南", "准备工作")
    assert first[0].segment_key == changed[0].segment_key
    assert first[0].content_hash != changed[0].content_hash


async def test_pdf_segments_keep_page_locator():
    drafts = _drafts(
        "第一页包含完整的部署准备说明和网络配置要求。\n第 1 页 / 共 2 页\n"
        "第二页包含完整的安装步骤说明和验收检查要求。\n第 2 页 / 共 2 页",
        filename="guide.pdf",
    )

    assert [draft.locator["page"] for draft in drafts] == [1, 2]
    assert drafts[0].locator["page_count"] == 2


async def test_ppt_segments_keep_approximate_slide_locator():
    drafts = _drafts(
        "# 公司简介\n我们提供覆盖咨询、实施、交付和运维的完整服务能力。\n"
        "![slide-1](slide-1.png)\n"
        "# 产品能力\n产品支持知识加工、内容审核、智能问答和来源追溯。",
        filename="intro.pptx",
    )

    assert drafts
    assert all(draft.segment_type == "slide" for draft in drafts)
    assert all(draft.locator.get("slide") for draft in drafts)


async def test_faq_question_and_answer_stay_in_one_segment():
    drafts = _drafts("# 常见问题\n\n如何重置密码？\n请在个人设置的安全页面中重置密码，并重新登录系统。")

    assert len(drafts) == 1
    assert drafts[0].segment_type == "qa"
    assert "如何重置密码" in drafts[0].content
    assert "安全页面" in drafts[0].content


async def test_large_markdown_table_is_split_with_repeated_headers():
    rows = ["| 名称 | 配置 | 说明 |", "| --- | --- | --- |"]
    rows.extend(f"| 产品{i} | 配置{i} | " + "这是详细说明" * 40 + " |" for i in range(30))

    drafts = _drafts("\n".join(rows), filename="products.xlsx")

    assert len(drafts) > 1
    assert all(draft.segment_type == "table" for draft in drafts)
    assert all(draft.content.startswith("| 名称 | 配置 | 说明 |\n| --- | --- | --- |") for draft in drafts)
    assert drafts[0].locator["row_start"] < drafts[-1].locator["row_start"]


async def test_media_reference_without_text_does_not_create_segment():
    assert _drafts("![only-image](image.png)", filename="image.png") == []


@pytest.fixture
async def segment_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
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
            title="部署指南.docx",
            path_text="产品资料 / 部署指南.docx",
            source_validity="valid",
        )
        version = FeishuMaterialVersion(
            version_id="version-1",
            item_id="item-1",
            revision="1",
            content_hash="hash-1",
            processing_status="parsed",
            review_status="pending",
            yuxi_file_id="file-1",
        )
        session.add_all([source, item, version])
        await session.commit()
        yield session, version, item
    await engine.dispose()


async def test_replace_for_version_is_idempotent_and_resets_changed_content(segment_session):
    session, version, item = segment_session
    service = SourceSegmentService(session)
    content = "# 部署要求\n\n生产环境至少需要四核处理器和八 GB 内存，并开放服务端口。"

    first = await service.replace_for_version(version, item, yuxi_file_id="file-1", content=content)
    first[0].publication_state = "INCLUDED"
    await session.flush()
    repeated = await service.replace_for_version(version, item, yuxi_file_id="file-1", content=content)

    assert len(first) == len(repeated) == 1
    assert first[0].segment_id == repeated[0].segment_id
    assert repeated[0].publication_state == "INCLUDED"

    changed = await service.replace_for_version(
        version,
        item,
        yuxi_file_id="file-1",
        content="# 部署要求\n\n生产环境至少需要八核处理器和十六 GB 内存，并开放服务端口。",
    )

    assert changed[0].segment_id == first[0].segment_id
    assert changed[0].publication_state == "PENDING"


async def test_publish_chunks_exclude_alias_and_excluded_segments_and_keep_trace_tags(segment_session):
    session, version, item = segment_session
    service = SourceSegmentService(session)
    segments = await service.replace_for_version(
        version,
        item,
        yuxi_file_id="file-1",
        content=(
            "# 第一部分\n\n这是需要发布的正式知识内容，包括完整的部署前置条件和操作说明。\n\n"
            "# 第二部分\n\n这是来自其他文档的重复内容，只作为别名来源保存，不重复进入索引。\n\n"
            "# 第三部分\n\n这是明确不纳入知识库的内容，不应进入正式检索索引。\n\n"
            "# 第四部分\n\n这是仍在等待人工审核的内容，也不应提前进入正式检索索引。"
        ),
    )
    assert len(segments) == 4
    segments[0].publication_state = "INCLUDED"
    segments[1].publication_state = "ALIAS"
    segments[2].publication_state = "EXCLUDED"
    await session.flush()

    chunks = build_retrieval_chunks(segments, file_id="file-1", document_title="部署指南")

    assert len(chunks) == 1
    assert chunks[0]["tags"]["source_segment_ids"] == [segments[0].segment_id]
    assert segment_ids_from_chunk_tags(chunks[0]["tags"]) == (segments[0].segment_id,)
    assert "重复内容" not in chunks[0]["content"]
    assert "不纳入" not in chunks[0]["content"]
    assert "等待人工审核" not in chunks[0]["content"]


async def test_review_transition_only_updates_pending_active_segments(segment_session):
    session, version, item = segment_session
    service = SourceSegmentService(session)
    segments = await service.replace_for_version(
        version,
        item,
        yuxi_file_id="file-1",
        content=(
            "# 待处理\n\n这是等待审核决定的候选内容。\n\n"
            "# 已归并\n\n这是已经作为重复来源归并的内容。\n\n"
            "# 已排除\n\n这是已经明确排除的内容。"
        ),
    )
    segments[1].publication_state = "ALIAS"
    segments[2].publication_state = "EXCLUDED"
    await session.flush()

    changed = await service.transition_pending_publication_state(version.version_id, target_state="INCLUDED")

    assert changed == 1
    assert [segment.publication_state for segment in segments] == ["INCLUDED", "ALIAS", "EXCLUDED"]


async def test_duplicate_governance_uses_stable_segments_and_updates_publication_state(segment_session):
    session, source_version, source_item = segment_session
    target_item = FeishuSourceItem(
        item_id="item-2",
        source_id="source-1",
        item_key="page:item-2",
        item_type="pptx",
        title="产品介绍.pptx",
        path_text="产品资料 / 产品介绍.pptx",
        source_validity="valid",
    )
    target_version = FeishuMaterialVersion(
        version_id="version-2",
        item_id="item-2",
        revision="1",
        content_hash="hash-2",
        processing_status="parsed",
        review_status="pending",
        yuxi_file_id="file-2",
    )
    relation = FeishuCrossDocumentRelation(
        relation_id="relation-segments",
        comparison_key="version-1:version-2",
        source_version_id="version-1",
        target_version_id="version-2",
        relation_type="EXACT_DUPLICATE",
        status="open",
    )
    session.add_all([target_item, target_version, relation])
    await session.flush()
    shared = "公司简介：善达信息专注企业数字化服务，为客户提供知识管理、智能助手和持续运营服务。"
    source_segments = await SourceSegmentService(session).replace_for_version(
        source_version,
        source_item,
        yuxi_file_id="file-1",
        content=f"# 公司简介\n\n{shared}",
    )
    target_segments = await SourceSegmentService(session).replace_for_version(
        target_version,
        target_item,
        yuxi_file_id="file-2",
        content=f"# 公司简介\n\n{shared}",
    )

    service = DuplicateKnowledgeService(session)
    candidates = await service.get_relation_candidates(relation.relation_id)

    assert len(candidates["fragment_matches"]) == 1
    match = candidates["fragment_matches"][0]
    assert match["source_segment_id"] == source_segments[0].segment_id
    assert match["target_segment_id"] == target_segments[0].segment_id
    assert match["source_locator"]["block"] == 1

    await service.resolve_relation(
        relation.relation_id,
        DuplicateRelationResolutionRequest(request_id="segment-resolution", strategy="USE_SOURCE"),
        operator_id="admin-1",
    )
    refs = list(await session.scalars(select(FeishuKnowledgeSourceFragment)))

    assert source_segments[0].publication_state == "INCLUDED"
    assert target_segments[0].publication_state == "ALIAS"
    assert {ref.segment_id for ref in refs} == {source_segments[0].segment_id, target_segments[0].segment_id}
