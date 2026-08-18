from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.routers.feishu_knowledge_router import (
    FeishuReviewService,
    _enqueue_publish,
)
from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.governance.domain import ReviewAction, ReviewDecision
from yuxi.governance.schemas import ReviewResolveRequest
from yuxi.governance.service import GovernanceService
from yuxi.storage.postgres.models_business import User

governance = APIRouter(
    prefix="/governance",
    tags=["knowledge-governance"],
    dependencies=[Depends(get_admin_user)],
)


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
