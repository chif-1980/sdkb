import asyncio
import re
from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.routers.feishu_knowledge_router import (
    FeishuReviewService,
    _enqueue_comparison_backfill,
    _enqueue_publish,
    _parsed_content_loader,
)
from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.governance.domain import ReviewAction, ReviewDecision
from yuxi.governance.duplicate_knowledge_service import DuplicateKnowledgeService
from yuxi.governance.presentation_layout_service import (
    extract_pptx_layout,
    render_pptx_slide,
)
from yuxi.governance.document_layout_service import (
    build_document_layout,
    render_document_page,
    supported_layout_suffix,
)
from yuxi.governance.review_package_service import ReviewPackageService
from yuxi.governance.schemas import (
    DuplicateRelationResolutionRequest,
    ReviewPackageDraftRequest,
    ReviewLayoutEditRequest,
    ReviewPackageResolveRequest,
    ReviewPackageTransferRequest,
    ReviewResolveRequest,
    SourceChangeRequestCancelRequest,
)
from yuxi.governance.service import GovernanceService
from yuxi.governance.source_change_service import SourceChangeService
from yuxi.governance.source_segment_service import SourceSegmentService, source_segment_dict
from yuxi.knowledge.utils.kb_utils import parse_minio_url
from yuxi.storage.minio import get_minio_client
from yuxi.services.file_preview import convert_office_to_pdf
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)

governance = APIRouter(
    prefix="/governance",
    tags=["knowledge-governance"],
    dependencies=[Depends(get_admin_user)],
)


class ComparisonBackfillRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)


_PRESENTATION_SOURCE_CACHE: OrderedDict[str, bytes] = OrderedDict()
_PRESENTATION_PDF_CACHE: OrderedDict[str, bytes] = OrderedDict()
_PRESENTATION_LAYOUT_CACHE: OrderedDict[str, dict] = OrderedDict()
_PRESENTATION_SLIDE_CACHE: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_PRESENTATION_CONVERSION_LOCK = asyncio.Lock()
_DOCUMENT_SOURCE_CACHE: OrderedDict[str, bytes] = OrderedDict()
_DOCUMENT_PDF_CACHE: OrderedDict[str, bytes] = OrderedDict()
_DOCUMENT_LAYOUT_CACHE: OrderedDict[str, dict] = OrderedDict()
_DOCUMENT_PAGE_CACHE: OrderedDict[tuple[str, int], tuple[bytes, str]] = OrderedDict()
_DOCUMENT_CONVERSION_LOCK = asyncio.Lock()
_RELATION_LAYOUT_CACHE: OrderedDict[str, dict] = OrderedDict()
_RELATION_PAGE_CACHE: OrderedDict[tuple[str, str, int], tuple[bytes, str]] = OrderedDict()


def _remember(cache: OrderedDict, key, value, *, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


async def _version_source(
    version_id: str,
    db: AsyncSession,
) -> tuple[FeishuMaterialVersion, FeishuSourceItem, str, bytes]:
    row = (
        await db.execute(
            select(FeishuMaterialVersion, FeishuSourceItem)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .where(FeishuMaterialVersion.version_id == version_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到资料版本：{version_id}")
    version, source_item = row
    object_path = version.source_object_path or (version.processing_params or {}).get("object_path")
    if not object_path or not str(object_path).startswith("minio://"):
        raise HTTPException(status_code=404, detail="未找到资料原文件")
    filename = source_item.title or "source"
    suffix = PurePosixPath(filename).suffix.lower()
    if not suffix:
        item_type_suffix = f".{str(source_item.item_type or '').strip().lower().lstrip('.')}"
        object_suffix = PurePosixPath(str(object_path).split("?", 1)[0]).suffix.lower()
        suffix = item_type_suffix if supported_layout_suffix(item_type_suffix) else object_suffix
        if suffix:
            filename = f"{filename}{suffix}"
    if not supported_layout_suffix(suffix):
        raise HTTPException(status_code=400, detail=f"资料格式暂不支持版式还原：{suffix or '未知格式'}")
    content_hash = str(version.content_hash or len(object_path))
    cache_key = f"{version_id}:{content_hash}"
    content = _DOCUMENT_SOURCE_CACHE.get(cache_key)
    if content is None:
        bucket_name, object_name = parse_minio_url(object_path)
        content = await get_minio_client().adownload_file(bucket_name, object_name)
        _remember(_DOCUMENT_SOURCE_CACHE, cache_key, content, limit=8)
    return version, source_item, filename, content


async def _presentation_source(package_id: str, db: AsyncSession) -> tuple[dict, str, bytes]:
    try:
        package = await ReviewPackageService(db).get_package(package_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    version_id = package.get("source_version_id")
    if not version_id:
        raise HTTPException(status_code=404, detail="当前审核任务没有关联资料版本")
    row = (
        await db.execute(
            select(FeishuMaterialVersion, FeishuSourceItem)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .where(FeishuMaterialVersion.version_id == version_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="未找到演示文稿资料")
    version, source_item = row
    package["source_content_hash"] = version.content_hash
    filename = source_item.title or "presentation.pptx"
    if PurePosixPath(filename).suffix.lower() != ".pptx":
        raise HTTPException(status_code=400, detail="当前资料不是可还原版式的 PPTX 文件")
    object_path = version.source_object_path or (version.processing_params or {}).get("object_path")
    if not object_path or not str(object_path).startswith("minio://"):
        raise HTTPException(status_code=404, detail="未找到演示文稿原文件")
    cache_key = f"{version.version_id}:{version.content_hash}"
    content = _PRESENTATION_SOURCE_CACHE.get(cache_key)
    if content is None:
        bucket_name, object_name = parse_minio_url(object_path)
        content = await get_minio_client().adownload_file(bucket_name, object_name)
        _remember(_PRESENTATION_SOURCE_CACHE, cache_key, content, limit=8)
    return package, filename, content


async def _document_source(package_id: str, db: AsyncSession) -> tuple[dict, str, bytes]:
    """Load one archived source for all supported visual review formats."""
    try:
        package = await ReviewPackageService(db).get_package(package_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    version_id = package.get("source_version_id")
    if not version_id:
        raise HTTPException(status_code=404, detail="当前审核任务没有关联资料版本")
    row = (
        await db.execute(
            select(FeishuMaterialVersion, FeishuSourceItem)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .where(FeishuMaterialVersion.version_id == version_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="未找到资料原文件")
    version, source_item = row
    filename = source_item.title or "source"
    suffix = PurePosixPath(filename).suffix.lower()
    if not supported_layout_suffix(suffix):
        raise HTTPException(status_code=400, detail="当前资料格式暂不支持版式还原")
    object_path = version.source_object_path or (version.processing_params or {}).get("object_path")
    if not object_path or not str(object_path).startswith("minio://"):
        raise HTTPException(status_code=404, detail="未找到资料原文件")
    content_hash = str(version.content_hash or len(object_path))
    package["source_content_hash"] = version.content_hash
    package["source_version_id"] = version_id
    cache_key = f"{version_id}:{content_hash}"
    content = _DOCUMENT_SOURCE_CACHE.get(cache_key)
    if content is None:
        bucket_name, object_name = parse_minio_url(object_path)
        content = await get_minio_client().adownload_file(bucket_name, object_name)
        _remember(_DOCUMENT_SOURCE_CACHE, cache_key, content, limit=8)
    return package, filename, content


async def _duplicate_service(db: AsyncSession, relation_id: str) -> DuplicateKnowledgeService:
    kb_id = await db.scalar(
        select(FeishuSource.target_kb_id)
        .join(FeishuSourceItem, FeishuSourceItem.source_id == FeishuSource.source_id)
        .join(FeishuMaterialVersion, FeishuMaterialVersion.item_id == FeishuSourceItem.item_id)
        .join(
            FeishuCrossDocumentRelation,
            FeishuCrossDocumentRelation.source_version_id == FeishuMaterialVersion.version_id,
        )
        .where(FeishuCrossDocumentRelation.relation_id == relation_id)
    )
    return DuplicateKnowledgeService(
        db,
        content_loader=_parsed_content_loader(kb_id) if kb_id else None,
    )


@governance.get("/review-packages")
async def list_review_packages(
    source_id: Annotated[str, Query(min_length=1)],
    view: Annotated[str, Query()] = "mine",
    workflow_status: Annotated[list[str] | None, Query()] = None,
    review_type: Annotated[list[str] | None, Query()] = None,
    problem_tag: Annotated[str | None, Query()] = None,
    risk_level: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        return await ReviewPackageService(db).list_packages(
            source_id,
            operator_id=current_user.uid,
            view=view,
            workflow_statuses=workflow_status,
            review_types=review_type,
            problem_tag=problem_tag,
            risk_level=risk_level,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@governance.get("/review-packages/{package_id}")
async def get_review_package(package_id: str, db: AsyncSession = Depends(get_db)):
    try:
        return await ReviewPackageService(db).get_package(package_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@governance.get("/review-packages/{package_id}/segments")
async def list_review_package_segments(package_id: str, db: AsyncSession = Depends(get_db)):
    try:
        package = await ReviewPackageService(db).get_package(package_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    version_id = package.get("source_version_id")
    if not version_id:
        return {"items": [], "count": 0, "token_count": 0}
    segments = await SourceSegmentService(db).list_active(version_id)
    return {
        "items": [source_segment_dict(segment) for segment in segments],
        "count": len(segments),
        "token_count": sum(segment.token_count or 0 for segment in segments),
    }


@governance.get("/review-packages/{package_id}/presentation")
async def get_review_package_presentation(package_id: str, db: AsyncSession = Depends(get_db)):
    package, _filename, content = await _presentation_source(package_id, db)
    version_id = package.get("source_version_id")
    cache_key = f"{version_id}:{package.get('source_content_hash') or len(content)}"
    cached_layout = _PRESENTATION_LAYOUT_CACHE.get(cache_key)
    if cached_layout is not None:
        _PRESENTATION_LAYOUT_CACHE.move_to_end(cache_key)
        return cached_layout
    segments = await SourceSegmentService(db).list_active(version_id)
    try:
        layout = extract_pptx_layout(content, segments=segments).as_dict()
        _remember(_PRESENTATION_LAYOUT_CACHE, cache_key, layout, limit=8)
        return layout
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"演示文稿版式读取失败：{exc}") from exc


@governance.get("/review-packages/{package_id}/presentation/slides/{slide_number}")
async def get_review_package_slide_preview(
    package_id: str,
    slide_number: int,
    db: AsyncSession = Depends(get_db),
):
    if slide_number < 1:
        raise HTTPException(status_code=400, detail="幻灯片页码必须从 1 开始")
    package, filename, content = await _presentation_source(package_id, db)
    version_id = str(package.get("source_version_id"))
    content_hash = str(package.get("source_content_hash") or len(content))
    pdf_cache_key = f"{version_id}:{content_hash}"
    cache_key = (pdf_cache_key, slide_number)
    image = _PRESENTATION_SLIDE_CACHE.get(cache_key)
    if image is None:
        try:
            pdf_content = _PRESENTATION_PDF_CACHE.get(pdf_cache_key)
            if pdf_content is None:
                async with _PRESENTATION_CONVERSION_LOCK:
                    pdf_content = _PRESENTATION_PDF_CACHE.get(pdf_cache_key)
                    if pdf_content is None:
                        pdf_content = await convert_office_to_pdf(filename, content)
                        _remember(_PRESENTATION_PDF_CACHE, pdf_cache_key, pdf_content, limit=8)
            image = await render_pptx_slide(
                content,
                filename=filename,
                slide_number=slide_number,
                pdf_content=pdf_content,
            )
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="未找到该页幻灯片") from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"幻灯片预览生成失败：{exc}") from exc
        _remember(_PRESENTATION_SLIDE_CACHE, cache_key, image, limit=48)
    return Response(
        content=image,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@governance.get("/review-packages/{package_id}/layout")
async def get_review_package_layout(package_id: str, db: AsyncSession = Depends(get_db)):
    package, filename, content = await _document_source(package_id, db)
    version_id = str(package.get("source_version_id"))
    content_hash = str(package.get("source_content_hash") or len(content))
    cache_key = f"{version_id}:{content_hash}:{filename}"
    layout = _DOCUMENT_LAYOUT_CACHE.get(cache_key)
    if layout is None:
        segments = await SourceSegmentService(db).list_active(version_id)
        try:
            layout = await build_document_layout(filename, content, segments=segments)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"资料版式读取失败：{exc}") from exc
        _remember(_DOCUMENT_LAYOUT_CACHE, cache_key, layout, limit=8)
    else:
        _DOCUMENT_LAYOUT_CACHE.move_to_end(cache_key)
    response = dict(layout)
    response["source_version_id"] = version_id
    response["edits"] = dict((package.get("draft") or {}).get("layout_edits") or {})
    return response


@governance.get("/review-packages/{package_id}/layout/pages/{page_number}")
async def get_review_package_layout_page(
    package_id: str,
    page_number: int,
    db: AsyncSession = Depends(get_db),
):
    if page_number < 1:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")
    package, filename, content = await _document_source(package_id, db)
    version_id = str(package.get("source_version_id"))
    content_hash = str(package.get("source_content_hash") or len(content))
    cache_key = (f"{version_id}:{content_hash}:{filename}", page_number)
    cached = _DOCUMENT_PAGE_CACHE.get(cache_key)
    if cached is None:
        try:
            suffix = PurePosixPath(filename).suffix.lower()
            pdf_content = None
            # Excel is rendered as an editable worksheet grid in the layout
            # response, so it does not need an Office-to-PDF conversion here.
            if suffix in {".docx", ".pptx"}:
                pdf_key = cache_key[0]
                pdf_content = _DOCUMENT_PDF_CACHE.get(pdf_key)
                if pdf_content is None:
                    async with _DOCUMENT_CONVERSION_LOCK:
                        pdf_content = _DOCUMENT_PDF_CACHE.get(pdf_key)
                        if pdf_content is None:
                            pdf_content = await convert_office_to_pdf(filename, content)
                            _remember(_DOCUMENT_PDF_CACHE, pdf_key, pdf_content, limit=8)
            cached = await render_document_page(
                filename,
                content,
                page_number=page_number,
                pdf_content=pdf_content,
            )
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="未找到该页资料") from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"资料页面预览生成失败：{exc}") from exc
        _remember(_DOCUMENT_PAGE_CACHE, cache_key, cached, limit=64)
    else:
        _DOCUMENT_PAGE_CACHE.move_to_end(cache_key)
    image, media_type = cached
    return Response(content=image, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@governance.patch("/review-packages/{package_id}/layout/edits")
async def save_review_package_layout_edit(
    package_id: str,
    payload: ReviewLayoutEditRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        result = await ReviewPackageService(db).save_layout_edit(
            package_id,
            operator_id=current_user.uid,
            lock_version=payload.lock_version,
            block_id=payload.block_id,
            page_number=payload.page_number,
            content=payload.content,
            source_segment_ids=payload.source_segment_ids,
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@governance.patch("/review-packages/{package_id}/draft")
async def save_review_package_draft(
    package_id: str,
    payload: ReviewPackageDraftRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        result = await ReviewPackageService(db).save_draft(
            package_id,
            operator_id=current_user.uid,
            lock_version=payload.lock_version,
            draft=payload.draft,
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@governance.post("/review-packages/{package_id}/transfer")
async def transfer_review_package(
    package_id: str,
    payload: ReviewPackageTransferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        result = await ReviewPackageService(db).transfer(
            package_id,
            operator_id=current_user.uid,
            lock_version=payload.lock_version,
            assignee_id=payload.assignee_id,
            comment=payload.comment,
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@governance.post("/review-packages/{package_id}/resolve")
async def resolve_review_package(
    package_id: str,
    payload: ReviewPackageResolveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        result = await ReviewPackageService(db).resolve(package_id, payload, operator_id=current_user.uid)
        unit_publish_queue: dict[str, bool] = {}
        if not result["idempotent_replay"]:
            review_service = FeishuReviewService(db)
            for candidate in result["reject_candidates"]:
                await review_service.reject(
                    candidate["version_id"],
                    operator_id=current_user.uid,
                    reason=candidate["reason"],
                )
            for version_id in result["publish_version_ids"]:
                await review_service.approve(version_id, operator_id=current_user.uid)
            for version_id in result["unit_publish_version_ids"]:
                queued = await review_service.queue_unit_publish(version_id, operator_id=current_user.uid)
                unit_publish_queue[version_id] = queued.enqueue_required
        await db.commit()

        publish_tasks = []
        if not result["idempotent_replay"]:
            for version_id in result["publish_version_ids"]:
                try:
                    task = await _enqueue_publish(version_id, operator_id=current_user.uid)
                    publish_tasks.append(task.id)
                except Exception as exc:
                    await FeishuReviewService(db).mark_publish_failed(version_id, message=str(exc))
                    await db.commit()
                    raise
            for version_id in result["unit_publish_version_ids"]:
                if not unit_publish_queue.get(version_id):
                    continue
                try:
                    task = await _enqueue_publish(version_id, operator_id=current_user.uid)
                    publish_tasks.append(task.id)
                except Exception as exc:
                    await FeishuReviewService(db).mark_publish_failed(version_id, message=str(exc))
                    await db.commit()
                    raise
        return {**result, "publish_task_ids": publish_tasks}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@governance.get("/reviewers")
async def list_reviewers(db: AsyncSession = Depends(get_db)):
    reviewers = list(
        await db.scalars(
            select(User)
            .where(User.role.in_({"admin", "superadmin"}), User.is_deleted == 0)
            .order_by(User.username.asc())
        )
    )
    return {"items": [{"user_id": user.uid, "name": user.username, "role": user.role} for user in reviewers]}


@governance.get("/source-change-requests")
async def list_source_change_requests(
    source_id: Annotated[str, Query(min_length=1)],
    status: Annotated[str | None, Query()] = None,
    responsible_user_id: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    db: AsyncSession = Depends(get_db),
):
    return await SourceChangeService(db).list_change_requests(
        source_id,
        status=status,
        responsible_user_id=responsible_user_id,
        page=page,
        page_size=page_size,
    )


@governance.get("/source-change-requests/{change_request_id}")
async def get_source_change_request(change_request_id: str, db: AsyncSession = Depends(get_db)):
    try:
        return await SourceChangeService(db).get_change_request(change_request_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@governance.post("/source-change-requests/{change_request_id}/cancel")
async def cancel_source_change_request(
    change_request_id: str,
    payload: SourceChangeRequestCancelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        result = await SourceChangeService(db).cancel_change_request(
            change_request_id,
            operator_id=current_user.uid,
            reason=payload.reason,
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@governance.get("/reviews")
async def list_reviews(
    source_id: Annotated[str, Query(min_length=1)],
    status: Annotated[str | None, Query()] = None,
    problem_tag: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
):
    items = await GovernanceService(db).list_reviews(source_id, status=status, problem_tag=problem_tag)
    return {"items": items}


@governance.get("/reviews/{review_id}")
async def get_review(review_id: str, db: AsyncSession = Depends(get_db)):
    try:
        return await GovernanceService(db).get_review(review_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@governance.get("/reviews/{review_id}/comparisons")
async def list_review_comparisons(review_id: str, db: AsyncSession = Depends(get_db)):
    try:
        items = await GovernanceService(db).list_review_comparisons(review_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items}


@governance.post("/reviews/{review_id}/resolve")
async def resolve_review(
    review_id: str,
    payload: ReviewResolveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    service = GovernanceService(db)
    try:
        review, version, item = await service.prepare_resolution(review_id, operator_id=current_user.uid)
        publish_task = None
        if payload.decision == ReviewDecision.PUBLISH:
            block_reason = service.content_publish_block_reason(version)
            if block_reason:
                raise ValueError(block_reason)
            has_open_conflict = await service.has_open_conflict(version.version_id)
            if has_open_conflict and payload.action not in {
                ReviewAction.UPDATE,
                ReviewAction.SPLIT_BY_SCOPE,
            }:
                raise ValueError("存在未解决的跨文档冲突，请选择更新或按适用范围拆分后再发布")
            await service.record_resolution(review, version, item, payload, operator_id=current_user.uid)
            await FeishuReviewService(db).approve(version.version_id, operator_id=current_user.uid)
        elif payload.decision == ReviewDecision.REJECT:
            await FeishuReviewService(db).reject(
                version.version_id,
                operator_id=current_user.uid,
                reason=payload.decision_comment or "",
            )
        elif payload.decision == ReviewDecision.TRANSFER:
            assignee = await db.scalar(
                select(User).where(
                    User.uid == payload.assignee_id,
                    User.role.in_({"admin", "superadmin"}),
                    User.is_deleted == 0,
                )
            )
            if assignee is None:
                raise ValueError("Assignee is not an active knowledge administrator")

        if payload.decision != ReviewDecision.PUBLISH:
            await service.record_resolution(review, version, item, payload, operator_id=current_user.uid)
        await db.commit()

        if payload.decision == ReviewDecision.PUBLISH:
            try:
                publish_task = await _enqueue_publish(version.version_id, operator_id=current_user.uid)
            except Exception as exc:
                await FeishuReviewService(db).mark_publish_failed(version.version_id, message=str(exc))
                await db.commit()
                raise
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "review_id": review.review_id,
        "version_id": version.version_id,
        "decision": payload.decision,
        "action": payload.action,
        "status": review.status,
        "assignee_id": review.assignee_id,
        "publish_task_id": publish_task.id if publish_task is not None else None,
    }


@governance.get("/relations")
async def list_relations(
    source_id: Annotated[str, Query(min_length=1)],
    relation_type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
):
    items = await GovernanceService(db).list_relations(
        source_id,
        relation_type=relation_type,
        status=status,
    )
    return {"items": items}


@governance.get("/relations/{relation_id}/duplicate-candidates")
async def get_duplicate_relation_candidates(
    relation_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await (await _duplicate_service(db, relation_id)).get_relation_candidates(relation_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _comparison_page_number(locator: dict | None) -> int | None:
    locator = locator or {}
    for key in ("page", "slide", "sheet_index"):
        value = locator.get(key)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return None
    return None


def _comparison_block_ids(layout: dict, match: dict, side: str) -> tuple[int | None, list[str]]:
    locator = match.get(f"{side}_locator") or {}
    page_number = _comparison_page_number(locator)
    segment_id = match.get(f"{side}_segment_id")
    excerpt = str(match.get(f"{side}_overlap_excerpt") or match.get(f"{side}_excerpt") or "")
    normalized_excerpt = re.sub(r"\s+", "", excerpt).lower()
    pages = layout.get("pages") or []
    candidate_pages = [page for page in pages if page_number is None or page.get("page_number") == page_number]
    if not candidate_pages:
        candidate_pages = pages
    for page in candidate_pages:
        blocks = page.get("blocks") or []
        matched = [block for block in blocks if segment_id and segment_id in (block.get("source_segment_ids") or [])]
        if not matched and normalized_excerpt:
            matched = [
                block
                for block in blocks
                if normalized_excerpt[:40]
                and normalized_excerpt[:40] in re.sub(r"\s+", "", str(block.get("content") or "")).lower()
            ]
        if matched:
            return page.get("page_number"), [str(block.get("block_id")) for block in matched]
    return page_number, []


@governance.get("/relations/{relation_id}/layout-comparison")
async def get_relation_layout_comparison(
    relation_id: str,
    db: AsyncSession = Depends(get_db),
):
    relation = await db.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == relation_id)
    )
    if relation is None:
        raise HTTPException(status_code=404, detail=f"Cross-document relation not found: {relation_id}")
    cache_key = f"{relation_id}:{relation.source_version_id}:{relation.target_version_id}"
    cached = _RELATION_LAYOUT_CACHE.get(cache_key)
    if cached is not None:
        _RELATION_LAYOUT_CACHE.move_to_end(cache_key)
        return cached
    try:
        source_version, source_item, source_filename, source_content = await _version_source(
            relation.source_version_id, db
        )
        target_version, target_item, target_filename, target_content = await _version_source(
            relation.target_version_id, db
        )
        source_segments = await SourceSegmentService(db).list_active(relation.source_version_id)
        target_segments = await SourceSegmentService(db).list_active(relation.target_version_id)
        source_layout = await build_document_layout(source_filename, source_content, segments=source_segments)
        target_layout = await build_document_layout(target_filename, target_content, segments=target_segments)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"跨文档版式读取失败：{exc}") from exc

    matches: list[dict] = []
    if relation.relation_type in {"EXACT_DUPLICATE", "OVERLAP"}:
        try:
            candidates = await (await _duplicate_service(db, relation_id)).get_relation_candidates(relation_id)
            matches = list(candidates.get("fragment_matches") or [])
        except (LookupError, ValueError):
            matches = []
    enriched_matches = []
    for match in matches:
        source_page, source_block_ids = _comparison_block_ids(source_layout, match, "source")
        target_page, target_block_ids = _comparison_block_ids(target_layout, match, "target")
        enriched_matches.append(
            {
                **match,
                "source_page_number": source_page,
                "target_page_number": target_page,
                "source_block_ids": source_block_ids,
                "target_block_ids": target_block_ids,
            }
        )
    response = {
        "supported": bool(source_layout.get("supported") and target_layout.get("supported")),
        "relation_id": relation_id,
        "relation_type": relation.relation_type,
        "source": {
            "version_id": source_version.version_id,
            "title": source_item.title or source_filename,
            "path": source_item.path_text,
            "revision": source_version.revision,
            "filename": source_filename,
            "file_type": source_layout.get("file_type"),
            "page_count": source_layout.get("page_count", 0),
            "pages": source_layout.get("pages", []),
        },
        "target": {
            "version_id": target_version.version_id,
            "title": target_item.title or target_filename,
            "path": target_item.path_text,
            "revision": target_version.revision,
            "filename": target_filename,
            "file_type": target_layout.get("file_type"),
            "page_count": target_layout.get("page_count", 0),
            "pages": target_layout.get("pages", []),
        },
        "matches": enriched_matches,
        "message": None if enriched_matches else "当前关系暂未生成可定位的版式匹配片段，以下保留文字证据。",
    }
    _remember(_RELATION_LAYOUT_CACHE, cache_key, response, limit=8)
    return response


@governance.get("/relations/{relation_id}/layout-comparison/{side}/pages/{page_number}")
async def get_relation_layout_comparison_page(
    relation_id: str,
    side: str,
    page_number: int,
    db: AsyncSession = Depends(get_db),
):
    if side not in {"source", "target"}:
        raise HTTPException(status_code=400, detail="对比资料侧必须是 source 或 target")
    if page_number < 1:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")
    relation = await db.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == relation_id)
    )
    if relation is None:
        raise HTTPException(status_code=404, detail=f"Cross-document relation not found: {relation_id}")
    version_id = relation.source_version_id if side == "source" else relation.target_version_id
    cache_key = (relation_id, side, page_number)
    cached = _RELATION_PAGE_CACHE.get(cache_key)
    if cached is None:
        try:
            _version, _item, filename, content = await _version_source(version_id, db)
            image, media_type = await render_document_page(filename, content, page_number=page_number)
        except HTTPException:
            raise
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="未找到该对比页面") from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"对比页面预览生成失败：{exc}") from exc
        cached = (image, media_type)
        _remember(_RELATION_PAGE_CACHE, cache_key, cached, limit=48)
    else:
        _RELATION_PAGE_CACHE.move_to_end(cache_key)
    image, media_type = cached
    return Response(content=image, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@governance.post("/relations/{relation_id}/resolve-duplicate")
async def resolve_duplicate_relation(
    relation_id: str,
    payload: DuplicateRelationResolutionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        result = await (await _duplicate_service(db, relation_id)).resolve_relation(
            relation_id,
            payload,
            operator_id=current_user.uid,
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@governance.get("/comparisons/status")
async def get_comparison_status(
    source_id: Annotated[str, Query(min_length=1)],
    db: AsyncSession = Depends(get_db),
):
    if await db.scalar(select(FeishuSource.source_id).where(FeishuSource.source_id == source_id)) is None:
        raise HTTPException(status_code=404, detail=f"Feishu source not found: {source_id}")
    return await GovernanceService(db).get_comparison_status(source_id)


@governance.post("/comparisons/backfill", status_code=202)
async def backfill_comparisons(
    payload: ComparisonBackfillRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    if await db.scalar(select(FeishuSource.source_id).where(FeishuSource.source_id == payload.source_id)) is None:
        raise HTTPException(status_code=404, detail=f"Feishu source not found: {payload.source_id}")
    task, created = await _enqueue_comparison_backfill(payload.source_id, operator_id=current_user.uid)
    return {"task_id": task.id, "status": task.status, "created": created}


@governance.get("/knowledge")
async def list_formal_knowledge(
    source_id: Annotated[str, Query(min_length=1)],
    db: AsyncSession = Depends(get_db),
):
    return {"items": await GovernanceService(db).list_formal_knowledge(source_id)}


@governance.get("/knowledge/{knowledge_id}/relations")
async def list_knowledge_relations(knowledge_id: str, db: AsyncSession = Depends(get_db)):
    try:
        items = await GovernanceService(db).list_knowledge_relations(knowledge_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items}


@governance.get("/knowledge/{knowledge_id}/versions")
async def list_knowledge_versions(knowledge_id: str, db: AsyncSession = Depends(get_db)):
    try:
        items = await GovernanceService(db).list_knowledge_versions(knowledge_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items}
