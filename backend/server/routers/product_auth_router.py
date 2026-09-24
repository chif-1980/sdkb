"""Browser authentication endpoints for the enterprise assistant."""

from __future__ import annotations

import logging
import json
import os
import secrets
from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_product_user, get_required_user
from yuxi.product_chat.auth_service import (
    CLIENT_STATE_COOKIE,
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    STATE_TTL_SECONDS,
    ProductAuthError,
    ProductAuthService,
)
from yuxi.product_chat.schemas import (
    FeishuClientLoginRequest,
    FeishuQrLoginConfigResponse,
    ProductUserResponse,
    SessionResponse,
)
from yuxi.storage.postgres.models_business import User
from yuxi.storage.redis import get_async_redis_client
from yuxi.utils.auth_utils import AuthUtils

logger = logging.getLogger(__name__)
_FEISHU_CALLBACK_ROUTE_SUFFIXES = ("/auth/feishu/callback", "/auth/feishu/callback/")
_ASSISTANT_HANDOFF_PREFIX = "enterprise-assistant:manager-handoff:"
_ASSISTANT_HANDOFF_TTL_SECONDS = 60


class ProductAuthRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()
        if not self.path.endswith(_FEISHU_CALLBACK_ROUTE_SUFFIXES):
            return route_handler

        async def callback_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except Exception as exc:
                logger.error(
                    "event=product_auth_callback_unexpected_error exception_type=%s error_id=%s",
                    type(exc).__name__,
                    uuid4().hex,
                )
                return RedirectResponse(url="/login?error=FEISHU_OAUTH_FAILED", status_code=303)

        return callback_route_handler


product_auth = APIRouter(route_class=ProductAuthRoute)


def _is_production() -> bool:
    return os.environ.get("YUXI_ENV", "development").strip().lower() in {"prod", "production"}


def _assistant_origin() -> str:
    """Return the configured assistant origin without accepting a user URL."""
    from urllib.parse import urlsplit

    configured = os.environ.get("ENTERPRISE_ASSISTANT_URL", "").strip()
    if not configured:
        configured = os.environ.get("FEISHU_PRODUCT_REDIRECT_URI", "").strip()
    parsed = urlsplit(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProductAuthError("ASSISTANT_ORIGIN_NOT_CONFIGURED", 503)
    if _is_production() and parsed.scheme != "https":
        raise ProductAuthError("ASSISTANT_ORIGIN_NOT_CONFIGURED", 503)
    return f"{parsed.scheme}://{parsed.netloc}"


def _safe_return_path(path: str | None) -> str:
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/chat"
    return path


async def get_product_auth_service(db: AsyncSession = Depends(get_db)) -> ProductAuthService:
    try:
        redis_client = await get_async_redis_client()
    except Exception:
        redis_client = None
    return ProductAuthService(db=db, redis_client=redis_client)


@product_auth.get("/auth/feishu/login")
async def feishu_login(
    return_path: str = "/chat",
    service: ProductAuthService = Depends(get_product_auth_service),
) -> RedirectResponse:
    try:
        login_url = await service.create_login_url(return_path)
    except ProductAuthError as exc:
        error_query = urlencode({"error": exc.code})
        return RedirectResponse(url=f"/login?{error_query}", status_code=303)
    return RedirectResponse(url=login_url, status_code=307)


@product_auth.post("/auth/assistant/handoff")
async def create_assistant_handoff(
    current_user: User = Depends(get_required_user),
    service: ProductAuthService = Depends(get_product_auth_service),
) -> dict:
    """Create a one-use handoff from the manager Bearer session to the assistant."""
    if service._redis is None:
        raise HTTPException(503, "登录交接暂不可用，请稍后重试")
    if current_user.is_deleted or current_user.is_login_locked() or current_user.department_id is None:
        raise HTTPException(403, "当前账号尚未完成企业身份配置")

    code = secrets.token_urlsafe(32)
    created = await service._redis.set(
        _ASSISTANT_HANDOFF_PREFIX + code,
        json.dumps({"user_id": current_user.id, "return_path": "/chat"}),
        ex=_ASSISTANT_HANDOFF_TTL_SECONDS,
        nx=True,
    )
    if not created:
        raise HTTPException(503, "登录交接暂不可用，请重试")
    try:
        origin = _assistant_origin()
    except ProductAuthError as exc:
        await service._redis.delete(_ASSISTANT_HANDOFF_PREFIX + code)
        raise HTTPException(exc.status_code, "企业知识助手地址尚未配置") from exc
    return {"url": f"{origin}/api/auth/assistant/callback?code={code}"}


@product_auth.get("/auth/assistant/callback")
async def complete_assistant_handoff(
    code: str | None = None,
    service: ProductAuthService = Depends(get_product_auth_service),
) -> RedirectResponse:
    """Exchange a short-lived handoff code for the assistant session cookie."""
    try:
        origin = _assistant_origin()
    except ProductAuthError:
        return RedirectResponse("/login?error=SSO_NOT_CONFIGURED", status_code=303)
    if service._redis is None or not code:
        return RedirectResponse(f"{origin}/login?error=SSO_EXPIRED", status_code=303)

    raw = await service._redis.getdel(_ASSISTANT_HANDOFF_PREFIX + code)
    if not raw:
        return RedirectResponse(f"{origin}/login?error=SSO_EXPIRED", status_code=303)
    try:
        payload = json.loads(raw)
        user_id = int(payload["user_id"])
        return_path = _safe_return_path(payload.get("return_path"))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return RedirectResponse(f"{origin}/login?error=SSO_INVALID", status_code=303)

    user = await service._db.scalar(select(User).where(User.id == user_id, User.is_deleted == 0))
    if user is None or user.department_id is None or user.is_login_locked():
        return RedirectResponse(f"{origin}/login?error=SSO_NOT_AUTHORIZED", status_code=303)

    session_token = AuthUtils.create_access_token(
        {"sub": str(user.id), "token_kind": "enterprise_assistant"},
        expires_delta=timedelta(seconds=SESSION_TTL_SECONDS),
    )
    response = RedirectResponse(f"{origin}{return_path}", status_code=303)
    _set_session_cookie(response, session_token)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@product_auth.get(
    "/auth/feishu/qr-config",
    response_model=FeishuQrLoginConfigResponse,
)
async def feishu_qr_config(
    response: Response,
    return_path: str = "/chat",
    service: ProductAuthService = Depends(get_product_auth_service),
) -> FeishuQrLoginConfigResponse:
    try:
        goto = await service.create_qr_login_url(return_path)
    except ProductAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code},
            headers={"Cache-Control": "no-store"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return FeishuQrLoginConfigResponse(goto=goto, expires_in=STATE_TTL_SECONDS)


@product_auth.get("/auth/feishu/callback")
@product_auth.get("/auth/feishu/callback/", include_in_schema=False)
async def feishu_callback(
    code: str | None = None,
    state: str | None = None,
    service: ProductAuthService = Depends(get_product_auth_service),
) -> RedirectResponse:
    try:
        user, session_token = await service.complete_callback(code=code, state=state)
        if getattr(service, "manager_context", None):
            from server.routers.feishu_manager_auth_router import manager_callback_response

            return await manager_callback_response(service, user)
    except ProductAuthError as exc:
        error_query = urlencode({"error": exc.code})
        context = getattr(service, "manager_context", None)
        if context:
            from server.routers.feishu_manager_auth_router import manager_origins

            if context["origin"] in manager_origins():
                return RedirectResponse(url=f"{context['origin']}/auth/feishu/callback#{error_query}", status_code=303)
        return RedirectResponse(url=f"/login?{error_query}", status_code=303)

    response = RedirectResponse(url=getattr(service, "return_path", "/chat"), status_code=303)
    _set_session_cookie(response, session_token)
    return response


@product_auth.get("/auth/feishu/client-config")
async def feishu_client_config(
    response: Response,
    return_path: str = "/chat",
    service: ProductAuthService = Depends(get_product_auth_service),
) -> dict:
    try:
        config = await service.create_client_config(return_path)
    except ProductAuthError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code}) from exc
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        CLIENT_STATE_COOKIE, config["state"], max_age=STATE_TTL_SECONDS,
        httponly=True, secure=_is_production(), samesite="strict", path="/api/auth/feishu",
    )
    return config


@product_auth.post("/auth/feishu/client-login")
async def feishu_client_login(
    payload: FeishuClientLoginRequest,
    request: Request,
    response: Response,
    service: ProductAuthService = Depends(get_product_auth_service),
) -> dict:
    try:
        _, token = await service.complete_client_login(
            payload.code, payload.state, request.cookies.get(CLIENT_STATE_COOKIE)
        )
    except ProductAuthError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code}) from exc
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(CLIENT_STATE_COOKIE, path="/api/auth/feishu", secure=_is_production(),
                           httponly=True, samesite="strict")
    _set_session_cookie(response, token)
    return {"returnPath": service.return_path}


def _set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )


@product_auth.post("/auth/logout", status_code=204)
async def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        secure=_is_production(),
        httponly=True,
        samesite="lax",
    )
    return response


@product_auth.get("/session", response_model=SessionResponse)
async def session(current_user: User = Depends(get_product_user)) -> SessionResponse:
    return SessionResponse(
        user=ProductUserResponse(
            id=str(current_user.id),
            name=current_user.username,
            avatar_url=current_user.avatar,
        )
    )
