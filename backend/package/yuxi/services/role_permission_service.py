"""Unified role and data-scope permission resolution."""

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import (
    FeishuDepartmentRole,
    Role,
    RoleCapability,
    RolePermission,
    User,
    UserRole,
)
from yuxi.storage.postgres.models_product import FeishuDepartmentBinding, FeishuUserDepartmentMembership

SCOPES = ("none", "self", "department", "all")
MODULES = {
    "admin": "系统管理",
    "users": "用户与部门",
    "workspace": "工作区",
    "agents": "智能体管理",
    "knowledge": "知识库",
    "feishu_knowledge": "飞书知识源",
    "governance": "知识治理",
    "meetings": "会议管理",
    "feedback": "用户反馈",
    "evaluation": "知识评估",
    "graph": "知识图谱",
    "tasks": "后台任务",
    "models": "模型配置",
    "extensions": "扩展管理",
    "dashboard": "数据总览",
}


async def permission_resolution(db: AsyncSession, user: User) -> dict:
    """One resolver for enforcement and the management preview; preserve legacy fallback."""
    empty = {f"{module}.{action}": "none" for module in MODULES for action in ("view", "manage")}
    if user.role == "superadmin":
        grants = dict.fromkeys(empty, "all")
        return {
            "permissions": grants,
            "sources": [{"kind": "system", "name": "超级管理员", "permissions": grants}],
            "legacy_fallback": False,
        }

    direct = set((await db.scalars(select(UserRole.role_key).where(UserRole.user_id == user.id))).all())
    inherited = (
        await db.execute(
            select(FeishuDepartmentRole.role_key, FeishuDepartmentBinding.id, FeishuDepartmentBinding.display_name)
            .join(FeishuDepartmentBinding, FeishuDepartmentBinding.id == FeishuDepartmentRole.department_binding_id)
            .join(
                FeishuUserDepartmentMembership,
                FeishuUserDepartmentMembership.department_binding_id == FeishuDepartmentBinding.id,
            )
            .where(FeishuUserDepartmentMembership.user_id == user.id)
        )
    ).all()
    role_keys = direct | {row[0] for row in inherited}
    sources = []
    if role_keys:
        roles = (await db.scalars(select(Role).where(Role.role_key.in_(role_keys)))).all()
        capabilities = (await db.scalars(select(RoleCapability).where(RoleCapability.role_key.in_(role_keys)))).all()
        for role in roles:
            grants = {item.permission: item.scope for item in capabilities if item.role_key == role.role_key}
            if role.role_key in direct:
                sources.append({"kind": "direct", "role_key": role.role_key, "name": role.name, "permissions": grants})
            for key, department_id, department_name in inherited:
                if key == role.role_key:
                    sources.append(
                        {
                            "kind": "department",
                            "role_key": key,
                            "name": role.name,
                            "department_id": department_id,
                            "department_name": department_name,
                            "permissions": grants,
                        }
                    )
    legacy = False
    if not role_keys and user.role in {"admin", "user"}:
        template = (await db.scalars(select(RoleCapability).where(RoleCapability.role_key == user.role))).all()
        if template:
            sources.append(
                {
                    "kind": "system",
                    "role_key": user.role,
                    "name": "管理员默认权限" if user.role == "admin" else "普通用户默认权限",
                    "permissions": {item.permission: item.scope for item in template},
                }
            )
        elif user.role == "admin":
            legacy = True
            grants = {
                f"{module}.{action}": "all"
                for module in MODULES
                if module not in {"feedback", "dashboard"}
                for action in ("view", "manage")
            }
            feedback = await db.get(RolePermission, ("admin", "feedback.view"), populate_existing=True)
            if feedback:
                grants["feedback.view"] = feedback.scope
            sources.append({"kind": "legacy", "name": "管理员兼容权限（未绑定角色时）", "permissions": grants})
    for source in sources:
        for key, scope in source["permissions"].items():
            if key in empty and scope in SCOPES:
                empty[key] = max((empty[key], scope), key=SCOPES.index)
    return {"permissions": empty, "sources": sources, "legacy_fallback": legacy}


async def permission_matrix(db: AsyncSession, user: User) -> dict[str, str]:
    return (await permission_resolution(db, user))["permissions"]


async def permission_scope(db: AsyncSession, user: User, permission: str) -> str:
    if user.role == "superadmin":
        return "all"
    scope = (await permission_matrix(db, user)).get(permission, "none")
    if permission.endswith(".manage") and scope in {"self", "department"}:
        return "none"
    return scope


async def feedback_scope(db: AsyncSession, user: User) -> str:
    return await permission_scope(db, user, "feedback.view")


def require_permission(permission: str):
    from server.utils.auth_middleware import get_db, get_required_user

    async def authenticated_user(
        user: User = Depends(get_required_user),
        db: AsyncSession = Depends(get_db),
    ):
        if await permission_scope(db, user, permission) == "none":
            raise HTTPException(status_code=403, detail="当前角色没有此项操作权限")
        return user

    return authenticated_user
