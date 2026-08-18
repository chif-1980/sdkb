from __future__ import annotations

from html import escape
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.integrations.feishu.user_oauth import FeishuUserOAuthError, FeishuUserOAuthService
from yuxi.storage.postgres.models_business import User
from yuxi.storage.redis import get_async_redis_client
from yuxi.utils.datetime_utils import utc_now

feishu_knowledge_oauth = APIRouter(prefix="/feishu-knowledge", tags=["feishu-knowledge-oauth"])


async def _oauth_service(db: AsyncSession) -> FeishuUserOAuthService:
    try:
        redis_client = await get_async_redis_client()
    except Exception as exc:
        raise FeishuUserOAuthError("FEISHU_OAUTH_STATE_UNAVAILABLE", 503, "飞书授权状态服务不可用") from exc
    return FeishuUserOAuthService(db=db, redis_client=redis_client)


@feishu_knowledge_oauth.post("/sources/{source_id}/oauth/authorize")
async def start_source_user_oauth(
    source_id: str,
    mode: Literal["redirect", "qr"] = Body(default="redirect", embed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        service = await _oauth_service(db)
        started_at = utc_now().isoformat()
        authorization_url = await service.create_authorization_url(
            source_id=source_id,
            operator_id=current_user.uid,
            mode=mode,
        )
    except FeishuUserOAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {"authorization_url": authorization_url, "started_at": started_at}


@feishu_knowledge_oauth.get("/sources/{source_id}/oauth/status")
async def source_user_oauth_status(
    source_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_admin_user),
):
    try:
        service = FeishuUserOAuthService(db=db)
        return await service.get_authorization_status(source_id)
    except FeishuUserOAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@feishu_knowledge_oauth.get("/oauth/callback")
async def complete_source_user_oauth(
    code: str | None = None,
    state: str | None = None,
    flow: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        service = await _oauth_service(db)
        credential = await service.complete_authorization(code=code, state=state)
        await db.commit()
    except FeishuUserOAuthError as exc:
        await db.rollback()
        if flow == "qr":
            return _qr_result_page(success=False, error_code=exc.code)
        query = urlencode({"oauth_status": "error", "oauth_error": exc.code})
        return RedirectResponse(url=f"/feishu-knowledge?{query}", status_code=303)
    except Exception:
        await db.rollback()
        if flow == "qr":
            return _qr_result_page(success=False, error_code="FEISHU_USER_OAUTH_FAILED")
        query = urlencode({"oauth_status": "error", "oauth_error": "FEISHU_USER_OAUTH_FAILED"})
        return RedirectResponse(url=f"/feishu-knowledge?{query}", status_code=303)

    if flow == "qr":
        return _qr_result_page(success=True)
    query = urlencode({"oauth_status": "success", "source_id": credential.source_id})
    return RedirectResponse(url=f"/feishu-knowledge?{query}", status_code=303)


def _qr_result_page(*, success: bool, error_code: str | None = None) -> HTMLResponse:
    title = "授权成功" if success else "授权未完成"
    detail = (
        "电脑端正在自动刷新知识目录，现在可以关闭此页面。" if success else "请关闭此页面，在电脑端重新发起扫码授权。"
    )
    status_class = "success" if success else "error"
    safe_error = escape(error_code or "")
    error_line = f'<p class="error-code">错误代码：{safe_error}</p>' if safe_error else ""
    html = f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
        color: #172033; background: #f4f8fd;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        width: min(100%, 420px); padding: 32px 24px; text-align: center; background: #fff;
        border: 1px solid #dce8f5; border-radius: 8px; box-shadow: 0 12px 36px rgba(44, 95, 145, .10);
      }}
      .mark {{
        width: 52px; height: 52px; margin: 0 auto 18px; display: grid; place-items: center;
        border-radius: 50%; color: #fff; font-size: 28px;
        background: {"#3f83c5" if success else "#d05252"};
      }}
      h1 {{ margin: 0 0 10px; font-size: 22px; letter-spacing: 0; }}
      p {{ margin: 0; color: #65738a; font-size: 15px; line-height: 1.7; }}
      .error-code {{ margin-top: 14px; color: #a33a3a; font-size: 12px; overflow-wrap: anywhere; }}
    </style>
  </head>
  <body>
    <main class="{status_class}">
      <div class="mark">{"✓" if success else "!"}</div>
      <h1>{title}</h1>
      <p>{detail}</p>
      {error_line}
    </main>
  </body>
</html>"""
    return HTMLResponse(content=html, status_code=200 if success else 400)


__all__ = ["feishu_knowledge_oauth"]
