from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.routers import feishu_knowledge_oauth_router as router
from yuxi.integrations.feishu.user_oauth import FeishuUserOAuthError

pytestmark = pytest.mark.asyncio


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


async def test_start_source_oauth_returns_feishu_authorization_url(monkeypatch) -> None:
    service = SimpleNamespace(
        create_authorization_url=AsyncMock(return_value="https://accounts.feishu.cn/oauth?state=one-time")
    )
    monkeypatch.setattr(router, "_oauth_service", AsyncMock(return_value=service))

    response = await router.start_source_user_oauth(
        "source-1",
        mode="redirect",
        db=FakeSession(),
        current_user=SimpleNamespace(uid="admin-1"),
    )

    assert response["authorization_url"] == "https://accounts.feishu.cn/oauth?state=one-time"
    assert response["started_at"]
    service.create_authorization_url.assert_awaited_once_with(
        source_id="source-1",
        operator_id="admin-1",
        mode="redirect",
    )


async def test_start_source_qr_oauth_selects_qr_redirect_mode(monkeypatch) -> None:
    service = SimpleNamespace(
        create_authorization_url=AsyncMock(return_value="https://accounts.feishu.cn/oauth?state=qr")
    )
    monkeypatch.setattr(router, "_oauth_service", AsyncMock(return_value=service))

    response = await router.start_source_user_oauth(
        "source-1",
        mode="qr",
        db=FakeSession(),
        current_user=SimpleNamespace(uid="admin-1"),
    )

    assert response["started_at"]
    service.create_authorization_url.assert_awaited_once_with(
        source_id="source-1",
        operator_id="admin-1",
        mode="qr",
    )


async def test_oauth_callback_commits_credential_and_redirects_without_tokens(monkeypatch) -> None:
    session = FakeSession()
    service = SimpleNamespace(complete_authorization=AsyncMock(return_value=SimpleNamespace(source_id="source-1")))
    monkeypatch.setattr(router, "_oauth_service", AsyncMock(return_value=service))

    response = await router.complete_source_user_oauth(code="oauth-code", state="one-time", db=session)

    assert response.status_code == 303
    assert response.headers["location"] == "/feishu-knowledge?oauth_status=success&source_id=source-1"
    assert "oauth-code" not in response.headers["location"]
    assert "access_token" not in response.headers["location"]
    assert session.commits == 1
    assert session.rollbacks == 0


async def test_oauth_callback_rolls_back_and_exposes_only_stable_error_code(monkeypatch) -> None:
    session = FakeSession()
    service = SimpleNamespace(
        complete_authorization=AsyncMock(
            side_effect=FeishuUserOAuthError(
                "FEISHU_USER_SOURCE_PERMISSION_DENIED",
                424,
                "secret diagnostic must not be redirected",
            )
        )
    )
    monkeypatch.setattr(router, "_oauth_service", AsyncMock(return_value=service))

    response = await router.complete_source_user_oauth(code="oauth-code", state="one-time", db=session)

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/feishu-knowledge?oauth_status=error&oauth_error=FEISHU_USER_SOURCE_PERMISSION_DENIED"
    )
    assert "secret" not in response.headers["location"]
    assert session.commits == 0
    assert session.rollbacks == 1


async def test_qr_oauth_callback_renders_phone_completion_page(monkeypatch) -> None:
    session = FakeSession()
    service = SimpleNamespace(complete_authorization=AsyncMock(return_value=SimpleNamespace(source_id="source-1")))
    monkeypatch.setattr(router, "_oauth_service", AsyncMock(return_value=service))

    response = await router.complete_source_user_oauth(
        code="oauth-code",
        state="one-time",
        flow="qr",
        db=session,
    )

    assert response.status_code == 200
    assert "授权成功" in response.body.decode("utf-8")
    assert "oauth-code" not in response.body.decode("utf-8")
    assert session.commits == 1
