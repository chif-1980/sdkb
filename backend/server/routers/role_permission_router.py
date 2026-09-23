import json
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user, get_superadmin_user
from yuxi.services.role_permission_service import feedback_scope
from yuxi.storage.postgres.models_business import OperationLog, RolePermission, User

role_permissions = APIRouter(prefix="/role-permissions", tags=["Role permissions"])


class FeedbackPermissionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["none", "department", "all"]


@role_permissions.get("/me")
async def my_permissions(user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    return {"feedback.view": await feedback_scope(db, user)}


@role_permissions.get("")
async def read_permissions(user: User = Depends(get_superadmin_user), db: AsyncSession = Depends(get_db)):
    record = await db.get(RolePermission, ("admin", "feedback.view"))
    return {"superadmin": "all", "admin": record.scope if record else "none", "user": "none"}


@role_permissions.put("/admin/feedback.view")
async def update_feedback_permission(
    payload: FeedbackPermissionEdit,
    request: Request,
    user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    # Serialize policy edits so the audit records describe the actual previous value.
    await db.get(User, user.id, with_for_update=True)
    previous = await db.get(RolePermission, ("admin", "feedback.view"), populate_existing=True)
    before = previous.scope if previous else "none"
    statement = insert(RolePermission).values(role="admin", permission="feedback.view", scope=payload.scope)
    await db.execute(
        statement.on_conflict_do_update(index_elements=["role", "permission"], set_={"scope": payload.scope})
    )
    db.add(
        OperationLog(
            user_id=user.id,
            operation="修改角色权限",
            details=json.dumps(
                {"role": "admin", "permission": "feedback.view", "before": before, "after": payload.scope},
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
        )
    )
    await db.commit()
    return {"role": "admin", "permission": "feedback.view", "scope": payload.scope}
