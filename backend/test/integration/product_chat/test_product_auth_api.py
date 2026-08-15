from __future__ import annotations

import io
import logging
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers import product_auth_router
from server.routers.product_auth_router import get_product_auth_service, product_auth
from server.utils.access_log_middleware import AccessLogMiddleware
from server.utils.auth_middleware import get_db, get_required_user
from yuxi.product_chat.auth_service import ProductAuthError
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class FakeProductAuthService:
    def __init__(self, user: User):
        self.user = user
        self.callback_error: ProductAuthError | None = None

    async def create_login_url(self, return_path: str = "/chat") -> str:
        assert return_path == "/chat"
        return "https://accounts.feishu.cn/open-apis/authen/v1/authorize?app_id=test-app-id&state=opaque"

    async def complete_callback(self, code: str | None, state: str | None) -> tuple[User, str]:
        if self.callback_error:
            raise self.callback_error
        assert code == "oauth-code"
        assert state == "oauth-state"
        token = AuthUtils.create_access_token(
            {"sub": str(self.user.id), "token_kind": "enterprise_assistant"},
            expires_delta=timedelta(hours=8),
        )
        return self.user, token


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-that-is-long-and-stable")
    monkeypatch.setenv("YUXI_INSTANCE_ID", "product-auth-api-test")
    monkeypatch.setenv("YUXI_ENV", "development")


@pytest_asyncio.fixture()
async def api_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(
            username="Employee",
            uid="employee-001",
            avatar="https://img.example/avatar.png",
            password_hash="not-used-by-product-auth",
            role="user",
            department=Department(name="Engineering"),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        service = FakeProductAuthService(user)
        app = FastAPI()
        app.include_router(product_auth, prefix="/api")

        @app.get("/api/admin-probe")
        async def admin_probe(current_user: User = Depends(get_required_user)):
            return {"id": current_user.id}

        access_log = io.StringIO()
        access_logger = logging.Logger("product-auth-api-test")
        access_logger.addHandler(logging.StreamHandler(access_log))
        app.add_middleware(AccessLogMiddleware, logger=access_logger)

        async def override_db():
            yield db

        async def override_service():
            return service

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_product_auth_service] = override_service
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            yield client, service, user, access_log, app
    await engine.dispose()


async def test_login_redirects_to_feishu_authorize_without_app_secret(api_context):
    client, _, _, _, _ = api_context

    response = await client.get("/api/auth/feishu/login", params={"return_path": "/chat"})

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize")
    assert "secret" not in response.headers["location"]


async def test_callback_sets_product_cookie_and_redirects_to_chat(api_context, monkeypatch):
    client, _, _, _, _ = api_context
    monkeypatch.setenv("YUXI_ENV", "production")

    response = await client.get(
        "/api/auth/feishu/callback",
        params={"code": "oauth-code", "state": "oauth-state"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/chat"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("enterprise_assistant_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" in cookie


async def test_callback_redirects_with_stable_error_code_only(api_context):
    client, service, _, _, _ = api_context
    service.callback_error = ProductAuthError(
        code="FEISHU_OAUTH_STATE_INVALID",
        status_code=401,
        message="sensitive provider error",
    )

    response = await client.get("/api/auth/feishu/callback")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=FEISHU_OAUTH_STATE_INVALID"
    assert "sensitive" not in response.headers["location"]


async def test_session_rejects_bearer_admin_token_without_product_cookie(api_context):
    client, _, user, _, _ = api_context
    admin_token = AuthUtils.create_access_token({"sub": str(user.id), "role": "superadmin"})

    response = await client.get("/api/session", headers={"Authorization": f"Bearer {admin_token}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "LOGIN_REQUIRED"


async def test_session_rejects_wrong_token_kind_in_product_cookie(api_context):
    client, _, user, _, _ = api_context
    admin_token = AuthUtils.create_access_token({"sub": str(user.id), "role": "superadmin"})

    response = await client.get(
        "/api/session",
        cookies={"enterprise_assistant_session": admin_token},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "SESSION_INVALID"


async def test_session_returns_bound_product_user(api_context):
    client, _, user, _, _ = api_context
    token = AuthUtils.create_access_token({"sub": str(user.id), "token_kind": "enterprise_assistant"})

    response = await client.get(
        "/api/session",
        cookies={"enterprise_assistant_session": token},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user": {
            "id": str(user.id),
            "name": "Employee",
            "avatarUrl": "https://img.example/avatar.png",
        }
    }


async def test_logout_clears_product_cookie(api_context):
    client, _, _, _, _ = api_context

    response = await client.post(
        "/api/auth/logout",
        cookies={"enterprise_assistant_session": "existing-session"},
    )

    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith('enterprise_assistant_session=""')
    assert "Max-Age=0" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


async def test_callback_access_log_excludes_code_state_token_and_cookie(api_context):
    client, _, _, access_log, _ = api_context

    await client.get(
        "/api/auth/feishu/callback",
        params={"code": "secret-oauth-code", "state": "secret-oauth-state"},
    )

    logged = access_log.getvalue()
    assert "/api/auth/feishu/callback" in logged
    assert "secret-oauth-code" not in logged
    assert "secret-oauth-state" not in logged
    assert "enterprise_assistant_session" not in logged


async def test_product_session_token_is_rejected_by_existing_bearer_api(api_context):
    client, _, user, _, _ = api_context
    product_token = AuthUtils.create_access_token(
        {"sub": str(user.id), "token_kind": "enterprise_assistant"}
    )

    response = await client.get(
        "/api/admin-probe",
        headers={"Authorization": f"Bearer {product_token}"},
    )

    assert response.status_code == 401


async def test_callback_redirects_stably_when_redis_is_unavailable(api_context, monkeypatch):
    client, _, _, _, app = api_context
    app.dependency_overrides.pop(get_product_auth_service)

    async def unavailable_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(product_auth_router, "get_async_redis_client", unavailable_redis)

    response = await client.get(
        "/api/auth/feishu/callback",
        params={"code": "oauth-code", "state": "oauth-state"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=AUTH_SERVICE_UNAVAILABLE"
