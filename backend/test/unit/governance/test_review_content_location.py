"""Review content follows each version's file, not a source's mutable target KB."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.review_package_service import ReviewPackageService
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceItem,
    KnowledgeBase,
    KnowledgeFile,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("current_kb,previous_kb", [("target", "target"), ("parsed", "older"), (None, None)])
async def test_review_resolves_each_version_content_without_changing_publication_target(current_kb, previous_kb):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all(
                [KnowledgeBase(kb_id=kb, name=kb, kb_type="milvus") for kb in {"target", "parsed", "older"}]
            )
            session.add(
                FeishuSource(
                    source_id="source",
                    name="Source",
                    wiki_root_token="root",
                    target_kb_id="target",
                    credential_env_name="FEISHU_USER_OAUTH",
                )
            )
            session.add(
                FeishuSourceItem(
                    item_id="item",
                    source_id="source",
                    item_key="docx:item",
                    item_type="docx",
                    title="Document",
                    active_version_id="old",
                    source_validity="valid",
                )
            )
            for version, kb in [("new", current_kb), ("old", previous_kb)]:
                session.add(
                    FeishuMaterialVersion(
                        version_id=version,
                        item_id="item",
                        revision=version,
                        content_hash=version,
                        yuxi_file_id=f"file-{version}",
                        processing_status="awaiting_review",
                    )
                )
                if kb:
                    session.add(KnowledgeFile(file_id=f"file-{version}", kb_id=kb, filename=f"{version}.docx"))
            session.add(
                FeishuReviewPackage(
                    package_id="package",
                    package_key="package",
                    source_id="source",
                    source_item_id="item",
                    source_version_id="new",
                    title_snapshot="Document",
                )
            )
            session.add(
                FeishuReviewItem(
                    review_item_id="review",
                    package_id="package",
                    candidate_key="update",
                    review_type="UPDATE",
                    subject_type="MATERIAL_VERSION",
                    subject_id="new",
                )
            )
            await session.commit()

            detail = await ReviewPackageService(session).get_package("package")

            assert detail["target_kb_id"] == "target"
            assert detail["yuxi_file_id"] == "file-new"
            assert detail["content_kb_id"] == current_kb
            assert detail["previous_version"]["yuxi_file_id"] == "file-old"
            assert detail["previous_version"]["content_kb_id"] == previous_kb
    finally:
        await engine.dispose()
