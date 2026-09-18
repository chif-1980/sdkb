"""Feishu tenant directory for assignment; directory membership does not grant app access."""

from fastapi import HTTPException
from sqlalchemy import select

from yuxi.integrations.feishu.client import FeishuClient, FeishuClientError
from yuxi.storage.postgres.models_product import AuthorizationStatus, FeishuUserBinding


async def load_meeting_directory(db, user_id: int) -> dict:
    binding = await db.scalar(
        select(FeishuUserBinding).where(
            FeishuUserBinding.user_id == user_id,
            FeishuUserBinding.authorization_status == AuthorizationStatus.ACTIVE,
        )
    )
    if binding is None:
        raise HTTPException(403, "请先关联飞书企业身份，再选择企业成员。")
    client = None
    try:
        client = FeishuClient()
        # A deployment's application token must resolve the requester's exact identity.
        employee = await client.get_employee(binding.feishu_user_id)
        if employee.get("open_id") != binding.feishu_open_id:
            raise HTTPException(403, "当前通讯录应用与登录用户的飞书企业不匹配。")
        params = {"department_id_type": "open_department_id", "user_id_type": "user_id"}
        raw_departments = await client.list_contact_pages(
            "/open-apis/contact/v3/departments/0/children",
            {**params, "fetch_child": "true"},
        )
        departments = [{"id": "0", "parentId": None, "name": "企业组织"}]
        departments.extend(
            {
                "id": item["open_department_id"],
                "parentId": item.get("parent_department_id") or "0",
                "name": item.get("name") or "未命名部门",
            }
            for item in raw_departments
        )
        bindings = list(
            await db.scalars(
                select(FeishuUserBinding).where(
                    FeishuUserBinding.tenant_key == binding.tenant_key,
                    FeishuUserBinding.authorization_status == AuthorizationStatus.ACTIVE,
                )
            )
        )
        local_ids = {item.feishu_user_id: str(item.user_id) for item in bindings}
        users = {}
        for department in departments:
            employees = await client.list_contact_pages(
                "/open-apis/contact/v3/users/find_by_department",
                {**params, "department_id": department["id"]},
            )
            for item in employees:
                status = item.get("status") or {}
                if any(status.get(key) for key in ("is_frozen", "is_resigned", "is_exited", "is_unjoin")):
                    continue
                identity = item.get("user_id")
                if not identity:
                    raise ValueError("通讯录缺少员工标识，请检查权限")
                if identity not in users:
                    users[identity] = {
                        "userId": local_ids.get(identity),
                        "feishuUserId": identity,
                        "displayName": item.get("name") or item.get("en_name") or identity,
                        "englishName": item.get("en_name") or "",
                        "departmentIds": [],
                    }
                users[identity]["departmentIds"].append(department["id"])
        return {
            "departments": departments,
            "users": list(users.values()),
            "scopeNotice": "显示飞书应用通讯录授权范围内的部门和员工；缺少部门时，请管理员扩大通讯录可见范围。",
        }
    except (FeishuClientError, ValueError) as exc:
        raise HTTPException(503, "读取飞书企业通讯录失败，请管理员检查通讯录权限与可见范围后重试。") from exc
    finally:
        if client:
            await client.aclose()
