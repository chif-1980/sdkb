"""Feishu identity shared with the assistant; management still requires an admin role."""

import hashlib
import json
import os
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.routers.product_auth_router import get_product_auth_service
from server.utils.auth_middleware import get_required_user
from yuxi.product_chat.auth_service import FEISHU_AUTHORIZE_URL, ProductAuthError, ProductAuthService
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_product import FeishuUserBinding
from yuxi.utils.auth_utils import AuthUtils

manager_auth = APIRouter(prefix="/auth/feishu/manager", tags=["auth"])


def manager_origins():
    configured = os.getenv("FEISHU_MANAGER_ORIGINS", "").split(",")
    knowledge = urlsplit(os.getenv("FEISHU_KNOWLEDGE_REDIRECT_URI", ""))
    if knowledge.scheme and knowledge.netloc:
        configured.append(f"{knowledge.scheme}://{knowledge.netloc}")
    if os.getenv("YUXI_ENV", "development").lower() not in {"prod", "production"}:
        configured.extend(["http://localhost:5173", "http://127.0.0.1:5173"])
    return {value.strip().rstrip("/") for value in configured if value.strip()}


class LoginRequest(BaseModel):
    origin: str = Field(max_length=300)
    challenge: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExchangeRequest(BaseModel):
    code: str = Field(min_length=20, max_length=200)
    verifier: str = Field(min_length=32, max_length=128)


@manager_auth.get("/config")
async def config():
    return {"enabled": bool(os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_PRODUCT_REDIRECT_URI"))}


@manager_auth.post("/login")
async def login(data: LoginRequest, response: Response, service=Depends(get_product_auth_service)):
    if data.origin not in manager_origins():
        raise HTTPException(400, "当前知枢地址尚未配置飞书登录，请联系管理员")
    try:
        service._require_configuration(include_secret=False)
        state = await service._create_state("/chat", manager={"origin": data.origin, "challenge": data.challenge})
    except ProductAuthError as exc:
        raise HTTPException(exc.status_code, "飞书登录暂不可用，请检查配置或稍后重试") from exc
    response.headers["Cache-Control"] = "no-store"
    query = urlencode(
        {"app_id": service._app_id, "redirect_uri": service._redirect_uri, "response_type": "code", "state": state}
    )
    return {"login_url": f"{FEISHU_AUTHORIZE_URL}?{query}"}


async def manager_callback_response(service: ProductAuthService, user):
    """Only an opaque, short lived browser-bound code crosses origins, never a JWT."""
    from fastapi.responses import RedirectResponse

    context = service.manager_context
    origin = context["origin"]
    if origin not in manager_origins():
        raise ProductAuthError("MANAGER_ORIGIN_INVALID", 400)
    if user.role not in {"admin", "superadmin"} or user.is_deleted or user.is_login_locked():
        return RedirectResponse(f"{origin}/auth/feishu/callback#error=MANAGEMENT_ACCESS_REQUIRED", status_code=303)
    code = secrets.token_urlsafe(32)
    await service._redis.set(
        "feishu-manager:exchange:" + hashlib.sha256(code.encode()).hexdigest(),
        json.dumps({"user_id": user.id, "challenge": context["challenge"]}),
        ex=60,
        nx=True,
    )
    return RedirectResponse(
        f"{origin}/auth/feishu/callback#code={code}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@manager_auth.post("/exchange")
async def exchange(data: ExchangeRequest, response: Response, service=Depends(get_product_auth_service)):
    raw = await service._redis.getdel("feishu-manager:exchange:" + hashlib.sha256(data.code.encode()).hexdigest())
    if not raw:
        raise HTTPException(401, "登录已失效，请重新发起飞书登录")
    payload = json.loads(raw)
    if not secrets.compare_digest(payload["challenge"], hashlib.sha256(data.verifier.encode()).hexdigest()):
        raise HTTPException(401, "登录浏览器校验失败，请从本页面重新登录")
    user = await service._db.scalar(select(User).where(User.id == payload["user_id"], User.is_deleted == 0))
    if not user or user.role not in {"admin", "superadmin"} or user.is_login_locked():
        raise HTTPException(403, "此飞书账号尚无知枢管理权限，请联系管理员授权")
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": AuthUtils.create_access_token({"sub": str(user.id)}), "token_type": "bearer"}


@manager_auth.get("/identity")
async def identity(user=Depends(get_required_user), service=Depends(get_product_auth_service)):
    binding = await service._db.scalar(select(FeishuUserBinding).where(FeishuUserBinding.user_id == user.id))
    return {
        "linked": bool(binding),
        "name": binding.display_name if binding else None,
        "tenantKey": binding.tenant_key if binding else None,
        "status": binding.authorization_status if binding else None,
        "role": user.role,
    }
