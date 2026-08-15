"""Browser authentication endpoints for the enterprise assistant."""

from __future__ import annotations

import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_product_user
from yuxi.product_chat.auth_service import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    ProductAuthError,
    ProductAuthService,
)
from yuxi.product_chat.schemas import ProductUserResponse, SessionResponse
from yuxi.storage.postgres.models_business import User
from yuxi.storage.redis import get_async_redis_client

product_auth = APIRouter()


def _is_production() -> bool:
    return os.environ.get("YUXI_ENV", "development").strip().lower() in {"prod", "production"}


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


@product_auth.get("/auth/feishu/callback")
async def feishu_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    service: ProductAuthService = Depends(get_product_auth_service),
) -> RedirectResponse:
    request.scope["query_string"] = b""
    try:
        _, session_token = await service.complete_callback(code=code, state=state)
    except ProductAuthError as exc:
        error_query = urlencode({"error": exc.code})
        return RedirectResponse(url=f"/login?{error_query}", status_code=303)
    except Exception:
        return RedirectResponse(url="/login?error=FEISHU_OAUTH_FAILED", status_code=303)

    response = RedirectResponse(url="/chat", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )
    return response


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
