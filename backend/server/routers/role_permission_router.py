import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user, get_superadmin_user
from yuxi.services.role_permission_service import (
    MODULES,
    SCOPES,
    permission_matrix,
    permission_scope,
    permission_resolution,
)
from yuxi.storage.postgres.models_business import (
    FeishuDepartmentRole,
    OperationLog,
    Role,
    RoleCapability,
    RolePermission,
    User,
    UserRole,
)
from yuxi.storage.postgres.models_product import FeishuDepartmentBinding, FeishuUserDepartmentMembership
from yuxi.utils.datetime_utils import format_utc_datetime

role_permissions = APIRouter(prefix="/role-permissions", tags=["Role permissions"])


async def get_role_manager(user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    if await permission_scope(db, user, "admin.manage") != "all":
        raise HTTPException(status_code=403, detail="需要角色管理权限")
    return user


class RoleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)


class CapabilityEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permissions: dict[str, str]
    expected_permissions: dict[str, str] | None = None


class AssignmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_keys: list[str]
    expected_role_keys: list[str] | None = None


class DepartmentRoleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_keys: list[str]
    expected_role_keys: list[str] | None = None


class FeedbackPermissionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str


def _validate_permissions(permissions: dict[str, str]):
    allowed = {f"{module}.{action}" for module in MODULES for action in ("view", "manage")}
    if set(permissions) - allowed or any(scope not in SCOPES for scope in permissions.values()):
        raise HTTPException(status_code=422, detail="权限项或数据范围无效")
    if any(
        scope in {"self", "department"} and permission != "feedback.view" for permission, scope in permissions.items()
    ):
        raise HTTPException(status_code=422, detail="本人和本部门范围目前仅支持用户反馈查看")


async def _audit(db, actor, request, operation, details):
    db.add(
        OperationLog(
            user_id=actor.id,
            operation=operation,
            details=json.dumps(details, ensure_ascii=False),
            ip_address=request.client.host if request.client else None,
        )
    )


async def _existing_roles(db: AsyncSession, role_keys: list[str]):
    unique_keys = sorted(set(role_keys))
    if len(unique_keys) != len(role_keys):
        raise HTTPException(status_code=422, detail="角色不能重复")
    found = (
        set((await db.scalars(select(Role.role_key).where(Role.role_key.in_(unique_keys)))).all())
        if unique_keys
        else set()
    )
    if found != set(unique_keys):
        raise HTTPException(status_code=404, detail="角色不存在")
    builtin = await db.scalar(select(Role.role_key).where(Role.role_key.in_(unique_keys), Role.is_builtin.is_(True)))
    if builtin:
        raise HTTPException(status_code=422, detail="内置身份通过用户管理维护，请选择自定义角色")
    return unique_keys


@role_permissions.get("/me")
async def my_permissions(user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    return await permission_matrix(db, user)


@role_permissions.get("")
async def list_roles(user: User = Depends(get_role_manager), db: AsyncSession = Depends(get_db)):
    roles = (await db.scalars(select(Role).order_by(Role.is_builtin.desc(), Role.name))).all()
    capabilities = (await db.scalars(select(RoleCapability))).all()
    members = (await db.scalars(select(UserRole))).all()
    department_roles = (await db.scalars(select(FeishuDepartmentRole))).all()
    departments = (await db.scalars(select(FeishuDepartmentBinding))).all()
    memberships = (await db.scalars(select(FeishuUserDepartmentMembership))).all()
    users = (await db.scalars(select(User).where(User.is_deleted == 0).order_by(User.username))).all()
    capability_map: dict[str, dict[str, str]] = {role.role_key: {} for role in roles}
    for item in capabilities:
        capability_map.setdefault(item.role_key, {})[item.permission] = item.scope
    for role in roles:
        if role.is_builtin:
            identity = User(id=-1, role=role.role_key)
            capability_map[role.role_key] = await permission_matrix(db, identity)
    return {
        "modules": MODULES,
        "scopes": SCOPES,
        "roles": [
            {
                "role_key": role.role_key,
                "name": role.name,
                "description": role.description,
                "is_builtin": role.is_builtin,
                "permissions": capability_map.get(role.role_key, {}),
                "inherited_user_ids": sorted(
                    {
                        member.user_id
                        for member in memberships
                        if any(
                            binding.role_key == role.role_key
                            and binding.department_binding_id == member.department_binding_id
                            for binding in department_roles
                        )
                    }
                ),
                "user_ids": [item.user_id for item in members if item.role_key == role.role_key],
                "departments": [
                    {
                        "id": item.department_binding_id,
                        "name": next(
                            (
                                department.display_name
                                for department in departments
                                if department.id == item.department_binding_id
                            ),
                            str(item.department_binding_id),
                        ),
                    }
                    for item in department_roles
                    if item.role_key == role.role_key
                ],
            }
            for role in roles
        ],
        "users": [{"id": item.id, "username": item.username, "uid": item.uid, "role": item.role} for item in users],
        "feishu_departments": [
            {
                "id": item.id,
                "name": item.display_name,
                "tenant_key": item.tenant_key,
                "feishu_department_id": item.feishu_department_id,
                "updated_at": format_utc_datetime(item.updated_at),
                "user_ids": sorted(
                    {member.user_id for member in memberships if member.department_binding_id == item.id}
                ),
                "membership_updated_at": format_utc_datetime(
                    max(
                        (member.updated_at for member in memberships if member.department_binding_id == item.id),
                        default=None,
                    )
                ),
            }
            for item in departments
        ],
    }


@role_permissions.get("/users/{user_id}/preview")
async def preview_permissions(
    user_id: int, actor: User = Depends(get_role_manager), db: AsyncSession = Depends(get_db)
):
    target = await db.get(User, user_id)
    if target is None or target.is_deleted:
        raise HTTPException(status_code=404, detail="用户不存在")
    result = await permission_resolution(db, target)
    return {"user_id": target.id, "username": target.username, "base_role": target.role, **result}


@role_permissions.post("")
async def create_role(
    payload: RoleCreate, request: Request, user: User = Depends(get_role_manager), db: AsyncSession = Depends(get_db)
):
    duplicate = await db.scalar(select(Role.role_key).where(Role.name == payload.name))
    if duplicate:
        raise HTTPException(status_code=409, detail="角色名称已存在")
    role = Role(
        role_key=f"custom_{uuid4().hex[:16]}", name=payload.name, description=payload.description, is_builtin=False
    )
    db.add(role)
    await _audit(db, user, request, "创建角色", {"role_key": role.role_key, "name": role.name})
    await db.commit()
    return {"role_key": role.role_key, "name": role.name, "description": role.description}


@role_permissions.put("/{role_key}")
async def update_role(
    role_key: str,
    payload: RoleEdit,
    request: Request,
    user: User = Depends(get_role_manager),
    db: AsyncSession = Depends(get_db),
):
    role = await db.get(Role, role_key, with_for_update=True)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if role.is_builtin or payload.role_key != role_key:
        raise HTTPException(status_code=409, detail="内置角色标识和名称不可修改")
    duplicate = await db.scalar(select(Role.role_key).where(Role.name == payload.name, Role.role_key != role_key))
    if duplicate:
        raise HTTPException(status_code=409, detail="角色名称已存在")
    before = {"name": role.name, "description": role.description}
    role.name, role.description = payload.name, payload.description
    await _audit(
        db,
        user,
        request,
        "修改角色",
        {"role_key": role_key, "before": before, "after": {"name": role.name, "description": role.description}},
    )
    await db.commit()
    return {"role_key": role.role_key, "name": role.name, "description": role.description}


@role_permissions.delete("/{role_key}")
async def delete_role(
    role_key: str, request: Request, user: User = Depends(get_role_manager), db: AsyncSession = Depends(get_db)
):
    role = await db.get(Role, role_key, with_for_update=True)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if role.is_builtin:
        raise HTTPException(status_code=409, detail="内置角色不可删除")
    if user.role != "superadmin":
        has_admin_capability = await db.scalar(
            select(RoleCapability.role_key).where(
                RoleCapability.role_key == role_key,
                RoleCapability.permission == "admin.manage",
                RoleCapability.scope != "none",
            )
        )
        if has_admin_capability:
            raise HTTPException(status_code=403, detail="仅超级管理员可删除具有角色管理权限的角色")
    await _audit(db, user, request, "删除角色", {"role_key": role_key, "name": role.name})
    await db.delete(role)
    await db.commit()
    return {"deleted": role_key}


@role_permissions.put("/{role_key}/permissions")
async def set_role_permissions(
    role_key: str,
    payload: CapabilityEdit,
    request: Request,
    user: User = Depends(get_role_manager),
    db: AsyncSession = Depends(get_db),
):
    role = await db.get(Role, role_key, with_for_update=True)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if role.role_key == "superadmin":
        raise HTTPException(status_code=409, detail="超级管理员权限固定，不可修改")
    if role.is_builtin and user.role != "superadmin":
        raise HTTPException(status_code=403, detail="仅超级管理员可修改内置角色权限")
    _validate_permissions(payload.permissions)
    if user.role != "superadmin" and any(
        key.startswith("admin.") and scope != "none" for key, scope in payload.permissions.items()
    ):
        raise HTTPException(status_code=403, detail="仅超级管理员可授予角色管理权限")
    existing = (await db.scalars(select(RoleCapability).where(RoleCapability.role_key == role_key))).all()
    before = {item.permission: item.scope for item in existing}
    if role.is_builtin:
        before = await permission_matrix(db, User(id=-1, role=role_key))
    if user.role != "superadmin" and before.get("admin.manage", "none") != "none":
        raise HTTPException(status_code=403, detail="仅超级管理员可修改具有角色管理权限的角色")
    if payload.expected_permissions is not None:
        expected = {key: value for key, value in payload.expected_permissions.items() if value != "none"}
        if expected != {key: value for key, value in before.items() if value != "none"}:
            raise HTTPException(status_code=409, detail="权限已被其他管理员修改，请刷新后重新编辑")
    for module in MODULES:
        if (
            payload.permissions.get(f"{module}.manage", "none") != "none"
            and payload.permissions.get(f"{module}.view", "none") == "none"
        ):
            raise HTTPException(status_code=422, detail="管理权限必须同时具备查看权限")
    for child, parent in {
        "knowledge": "extensions",
        "evaluation": "extensions",
        "graph": "extensions",
        "governance": "feishu_knowledge",
    }.items():
        if (
            payload.permissions.get(f"{child}.view", "none") != "none"
            and payload.permissions.get(f"{parent}.view", "none") == "none"
        ):
            raise HTTPException(status_code=422, detail=f"请先开放{MODULES[parent]}入口，再配置{MODULES[child]}能力")
    if payload.permissions.get("feedback.manage") == "all" and payload.permissions.get("feedback.view") != "all":
        raise HTTPException(status_code=422, detail="反馈管理目前为全企业权限，请将查看范围设为全企业或使用仅查看")
    await db.execute(delete(RoleCapability).where(RoleCapability.role_key == role_key))
    # Persist explicit denials for templates, including an entirely empty template.
    # This distinguishes "no grants" from the pre-RBAC administrator defaults.
    saved = (
        {
            f"{module}.{action}": payload.permissions.get(f"{module}.{action}", "none")
            for module in MODULES
            for action in ("view", "manage")
        }
        if role.is_builtin
        else payload.permissions
    )
    db.add_all(
        RoleCapability(role_key=role_key, permission=permission, scope=scope)
        for permission, scope in saved.items()
        if role.is_builtin or scope != "none"
    )
    await _audit(
        db, user, request, "修改角色权限", {"role_key": role_key, "before": before, "after": payload.permissions}
    )
    await db.commit()
    return {"role_key": role_key, "permissions": payload.permissions}


@role_permissions.put("/users/{user_id}/roles")
async def set_user_roles(
    user_id: int,
    payload: AssignmentEdit,
    request: Request,
    user: User = Depends(get_role_manager),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, user_id, with_for_update=True)
    if target is None or target.is_deleted:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.role == "superadmin":
        raise HTTPException(status_code=409, detail="超级管理员的权限通过系统角色维护")
    role_keys = await _existing_roles(db, payload.role_keys)
    if user.role != "superadmin" and role_keys:
        has_admin_capability = await db.scalar(
            select(RoleCapability.role_key).where(
                RoleCapability.role_key.in_(role_keys),
                RoleCapability.permission == "admin.manage",
                RoleCapability.scope != "none",
            )
        )
        if has_admin_capability:
            raise HTTPException(status_code=403, detail="仅超级管理员可指派角色管理权限")
    existing = (await db.scalars(select(UserRole).where(UserRole.user_id == user_id))).all()
    before = sorted(item.role_key for item in existing)
    if user.role != "superadmin" and set(before) - set(role_keys):
        protected = await db.scalar(
            select(RoleCapability.role_key).where(
                RoleCapability.role_key.in_(set(before) - set(role_keys)),
                RoleCapability.permission == "admin.manage",
                RoleCapability.scope == "all",
            )
        )
        if protected:
            raise HTTPException(status_code=403, detail="仅超级管理员可移除角色管理授权")
    if payload.expected_role_keys is not None and sorted(payload.expected_role_keys) != before:
        raise HTTPException(status_code=409, detail="角色绑定已被其他管理员修改，请刷新后重试")
    await db.execute(delete(UserRole).where(UserRole.user_id == user_id))
    db.add_all(UserRole(user_id=user_id, role_key=key, assigned_by=user.id) for key in role_keys)
    await _audit(db, user, request, "指派用户角色", {"user_id": user_id, "before": before, "after": role_keys})
    await db.commit()
    return {"user_id": user_id, "role_keys": role_keys}


@role_permissions.put("/feishu-departments/{department_binding_id}/roles")
async def set_department_roles(
    department_binding_id: int,
    payload: DepartmentRoleEdit,
    request: Request,
    user: User = Depends(get_role_manager),
    db: AsyncSession = Depends(get_db),
):
    department = await db.get(FeishuDepartmentBinding, department_binding_id, with_for_update=True)
    if department is None:
        raise HTTPException(status_code=404, detail="飞书部门映射不存在")
    role_keys = await _existing_roles(db, payload.role_keys)
    if user.role != "superadmin" and role_keys:
        has_admin_capability = await db.scalar(
            select(RoleCapability.role_key).where(
                RoleCapability.role_key.in_(role_keys),
                RoleCapability.permission == "admin.manage",
                RoleCapability.scope != "none",
            )
        )
        if has_admin_capability:
            raise HTTPException(status_code=403, detail="仅超级管理员可映射角色管理权限")
    existing = (
        await db.scalars(
            select(FeishuDepartmentRole).where(FeishuDepartmentRole.department_binding_id == department_binding_id)
        )
    ).all()
    before = sorted(item.role_key for item in existing)
    if user.role != "superadmin" and set(before) - set(role_keys):
        protected = await db.scalar(
            select(RoleCapability.role_key).where(
                RoleCapability.role_key.in_(set(before) - set(role_keys)),
                RoleCapability.permission == "admin.manage",
                RoleCapability.scope == "all",
            )
        )
        if protected:
            raise HTTPException(status_code=403, detail="仅超级管理员可移除角色管理授权")
    if payload.expected_role_keys is not None and sorted(payload.expected_role_keys) != before:
        raise HTTPException(status_code=409, detail="角色绑定已被其他管理员修改，请刷新后重试")
    await db.execute(
        delete(FeishuDepartmentRole).where(FeishuDepartmentRole.department_binding_id == department_binding_id)
    )
    db.add_all(
        FeishuDepartmentRole(department_binding_id=department_binding_id, role_key=key, assigned_by=user.id)
        for key in role_keys
    )
    await _audit(
        db,
        user,
        request,
        "映射飞书部门角色",
        {"department_binding_id": department_binding_id, "before": before, "after": role_keys},
    )
    await db.commit()
    return {"department_binding_id": department_binding_id, "role_keys": role_keys}


@role_permissions.put("/admin/feedback.view")
async def update_legacy_feedback_permission(
    payload: FeedbackPermissionEdit,
    request: Request,
    user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.scope not in {"none", "department", "all"}:
        raise HTTPException(status_code=422, detail="数据范围无效")
    await db.get(Role, "admin", with_for_update=True)
    configured = await db.scalar(select(RoleCapability.role_key).where(RoleCapability.role_key == "admin").limit(1))
    if configured:
        raise HTTPException(status_code=409, detail="管理员已使用统一权限模板，请在角色权限页面修改")
    await db.get(User, user.id, with_for_update=True)
    previous = await db.get(RolePermission, ("admin", "feedback.view"), populate_existing=True)
    before = previous.scope if previous else "none"
    if previous is None:
        db.add(RolePermission(role="admin", permission="feedback.view", scope=payload.scope))
    else:
        previous.scope = payload.scope
    await _audit(
        db,
        user,
        request,
        "修改角色权限",
        {"role": "admin", "permission": "feedback.view", "before": before, "after": payload.scope},
    )
    await db.commit()
    return {"role": "admin", "permission": "feedback.view", "scope": payload.scope}
