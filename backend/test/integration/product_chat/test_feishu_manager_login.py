"""Real HTTP/DB boundary, fake external Feishu only; no external messages."""

import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.routers.feishu_manager_auth_router import manager_auth
from server.routers.product_auth_router import product_auth, get_product_auth_service
from yuxi.product_chat.auth_service import ProductAuthService
from yuxi.utils.auth_utils import AuthUtils
from test.unit.product_chat.test_product_auth_service import (  # noqa: F401
    db_session,
    auth_environment,
    FakeRedis,
    FakeDirectoryClient,
    _profile,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.parametrize("role", ["admin", "user"])
async def test_manager_login_shared_identity_browser_binding_and_replay(db_session, monkeypatch, role):  # noqa: F811
    monkeypatch.setenv("YUXI_ENV", "development")
    redis = FakeRedis()
    service = ProductAuthService(db=db_session, redis_client=redis, directory_client=FakeDirectoryClient())

    async def profile(code):
        return _profile()

    monkeypatch.setattr(service, "_fetch_profile", profile)
    user = await service.resolve_bound_user(_profile())
    user.role = role
    await db_session.commit()
    app = FastAPI()
    app.include_router(product_auth, prefix="/api")
    app.include_router(manager_auth, prefix="/api")
    app.dependency_overrides[get_product_auth_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:5173") as client:
        verifier = "browser-secret-" * 4
        challenge = hashlib.sha256(verifier.encode()).hexdigest()
        denied = await client.post(
            "/api/auth/feishu/manager/login", json={"origin": "https://evil.test", "challenge": challenge}
        )
        assert denied.status_code == 400

        async def start():
            response = await client.post(
                "/api/auth/feishu/manager/login", json={"origin": "http://localhost:5173", "challenge": challenge}
            )
            assert response.status_code == 200
            params = parse_qs(urlsplit(response.json()["login_url"]).query)
            assert params["redirect_uri"] == [service._redirect_uri]
            return params["state"][0]

        state = await start()
        callback = await client.get("/api/auth/feishu/callback", params={"state": state, "code": "external-code"})
        assert callback.status_code == 303
        assert "set-cookie" not in callback.headers
        assert callback.headers["location"].startswith("http://localhost:5173/auth/feishu/callback#")
        if role == "user":
            assert callback.headers["location"].endswith("error=MANAGEMENT_ACCESS_REQUIRED")
            assert user.role == "user"
            return
        code = parse_qs(urlsplit(callback.headers["location"]).fragment)["code"][0]
        response = await client.post("/api/auth/feishu/manager/exchange", json={"code": code, "verifier": verifier})
        assert response.status_code == 200
        claims = AuthUtils.verify_access_token(response.json()["access_token"])
        assert claims["sub"] == str(user.id) and "token_kind" not in claims
        assert (
            await client.post("/api/auth/feishu/manager/exchange", json={"code": code, "verifier": verifier})
        ).status_code == 401
        state = await start()
        callback = await client.get("/api/auth/feishu/callback", params={"state": state, "code": "external-code"})
        code = parse_qs(urlsplit(callback.headers["location"]).fragment)["code"][0]
        assert (
            await client.post("/api/auth/feishu/manager/exchange", json={"code": code, "verifier": "wrong-browser" * 4})
        ).status_code == 401
        # A role removed after the OAuth callback is rechecked at exchange time.
        state = await start()
        callback = await client.get("/api/auth/feishu/callback", params={"state": state, "code": "external-code"})
        code = parse_qs(urlsplit(callback.headers["location"]).fragment)["code"][0]
        user.role = "user"
        await db_session.commit()
        assert (
            await client.post("/api/auth/feishu/manager/exchange", json={"code": code, "verifier": verifier})
        ).status_code == 403
