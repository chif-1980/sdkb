from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from yuxi.integrations.feishu import user_oauth as user_oauth_module
from yuxi.integrations.feishu.schemas import FeishuNode
from yuxi.integrations.feishu.user_oauth import (
    FeishuTokenCipher,
    FeishuUserOAuthError,
    FeishuUserOAuthService,
)
from yuxi.storage.postgres.models_knowledge import FeishuProcessingEvent, FeishuUserOAuthCredential
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.asyncio


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, ex, nx):
        assert ex == 300
        assert nx is True
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def getdel(self, key):
        return self.values.pop(key, None)


class FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.flush_count = 0

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1


def oauth_env() -> dict[str, str]:
    return {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "app-secret",
        "FEISHU_KNOWLEDGE_REDIRECT_URI": ("http://127.0.0.1:5173/api/feishu-knowledge/oauth/callback"),
        "FEISHU_KNOWLEDGE_QR_REDIRECT_URI": ("http://172.16.26.50:5173/api/feishu-knowledge/oauth/callback?flow=qr"),
        "FEISHU_OAUTH_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    }


async def test_token_cipher_can_be_domain_derived_from_jwt_secret() -> None:
    first = FeishuTokenCipher.from_environ({"JWT_SECRET_KEY": "stable-random-jwt-secret"})
    second = FeishuTokenCipher.from_environ({"JWT_SECRET_KEY": "stable-random-jwt-secret"})

    ciphertext = first.encrypt("refresh-token")

    assert ciphertext != "refresh-token"
    assert second.decrypt(ciphertext) == "refresh-token"


async def test_authorization_url_uses_one_time_hashed_state_and_offline_scope(monkeypatch) -> None:
    redis = FakeRedis()
    service = FeishuUserOAuthService(db=FakeSession(), redis_client=redis, environ=oauth_env())
    monkeypatch.setattr(
        service,
        "_get_source",
        AsyncMock(return_value=SimpleNamespace(source_id="source-1")),
    )

    authorization_url = await service.create_authorization_url(source_id="source-1", operator_id="admin-1")
    query = parse_qs(urlparse(authorization_url).query)
    state = query["state"][0]

    assert query["scope"] == [
        "offline_access wiki:wiki:readonly docx:document:readonly drive:file:download"
    ]
    assert query["redirect_uri"] == ["http://127.0.0.1:5173/api/feishu-knowledge/oauth/callback"]
    assert state not in next(iter(redis.values))
    consumed = await service._consume_state(state)
    assert consumed["source_id"] == "source-1"
    assert consumed["operator_id"] == "admin-1"
    assert consumed["redirect_uri"] == ("http://127.0.0.1:5173/api/feishu-knowledge/oauth/callback")
    assert consumed["mode"] == "redirect"
    with pytest.raises(FeishuUserOAuthError, match="状态无效"):
        await service._consume_state(state)


async def test_qr_authorization_url_uses_phone_reachable_redirect_uri(monkeypatch) -> None:
    redis = FakeRedis()
    service = FeishuUserOAuthService(db=FakeSession(), redis_client=redis, environ=oauth_env())
    monkeypatch.setattr(
        service,
        "_get_source",
        AsyncMock(return_value=SimpleNamespace(source_id="source-1")),
    )

    authorization_url = await service.create_authorization_url(
        source_id="source-1",
        operator_id="admin-1",
        mode="qr",
    )
    query = parse_qs(urlparse(authorization_url).query)
    state = query["state"][0]

    assert query["redirect_uri"] == ["http://172.16.26.50:5173/api/feishu-knowledge/oauth/callback?flow=qr"]
    consumed = await service._consume_state(state)
    assert consumed["redirect_uri"] == query["redirect_uri"][0]
    assert consumed["mode"] == "qr"


async def test_qr_authorization_requires_a_phone_reachable_redirect(monkeypatch) -> None:
    environment = oauth_env()
    environment.pop("FEISHU_KNOWLEDGE_QR_REDIRECT_URI")
    service = FeishuUserOAuthService(
        db=FakeSession(),
        redis_client=FakeRedis(),
        environ=environment,
    )
    monkeypatch.setattr(
        service,
        "_get_source",
        AsyncMock(return_value=SimpleNamespace(source_id="source-1")),
    )

    with pytest.raises(FeishuUserOAuthError) as raised:
        await service.create_authorization_url(
            source_id="source-1",
            operator_id="admin-1",
            mode="qr",
        )

    assert raised.value.code == "FEISHU_USER_QR_OAUTH_NOT_CONFIGURED"
    assert raised.value.status_code == 503


async def test_source_access_validation_reads_docx_content(monkeypatch) -> None:
    root = FeishuNode(
        space_id="space-1",
        node_token="root-node",
        obj_token="root-document",
        obj_type="docx",
    )
    client = SimpleNamespace(
        get_node=AsyncMock(return_value=root),
        list_nodes=AsyncMock(return_value=[]),
        get_wiki_document=AsyncMock(),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(user_oauth_module, "FeishuClient", lambda **_kwargs: client)
    service = FeishuUserOAuthService(db=FakeSession(), environ=oauth_env())
    source = SimpleNamespace(wiki_root_token="root-node", scan_scope="space")

    await service._validate_source_access(source, "user-access-token")

    client.list_nodes.assert_awaited_once_with("space-1")
    client.get_wiki_document.assert_awaited_once_with(root)
    client.aclose.assert_awaited_once()


async def test_complete_authorization_encrypts_tokens_before_storage(monkeypatch) -> None:
    environment = oauth_env()
    redis = FakeRedis()
    session = FakeSession()
    service = FeishuUserOAuthService(db=session, redis_client=redis, environ=environment)
    source = SimpleNamespace(source_id="source-1", wiki_root_token="root", scan_scope="space")
    monkeypatch.setattr(service, "_get_source", AsyncMock(return_value=source))
    authorization_url = await service.create_authorization_url(source_id="source-1", operator_id="admin-1")
    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    monkeypatch.setattr(
        service,
        "_exchange_code",
        AsyncMock(
            return_value={
                "access_token": "plain-access-token",
                "refresh_token": "plain-refresh-token",
                "expires_in": 7200,
                "refresh_token_expires_in": 2_592_000,
                "scope": "offline_access wiki:wiki:readonly",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_fetch_profile",
        AsyncMock(return_value={"open_id": "ou_user", "name": "知识库管理员"}),
    )
    monkeypatch.setattr(service, "_validate_source_access", AsyncMock())
    monkeypatch.setattr(service, "_get_credential", AsyncMock(return_value=None))

    credential = await service.complete_authorization(code="oauth-code", state=state)

    cipher = FeishuTokenCipher.from_environ(environment)
    assert credential.access_token_ciphertext != "plain-access-token"
    assert credential.refresh_token_ciphertext != "plain-refresh-token"
    assert cipher.decrypt(credential.access_token_ciphertext) == "plain-access-token"
    assert cipher.decrypt(credential.refresh_token_ciphertext) == "plain-refresh-token"
    assert credential.authorization_status == "active"
    assert credential.display_name == "知识库管理员"
    service._exchange_code.assert_awaited_once_with(
        "oauth-code",
        redirect_uri="http://127.0.0.1:5173/api/feishu-knowledge/oauth/callback",
    )
    event = next(value for value in session.added if isinstance(value, FeishuProcessingEvent))
    assert "plain-access-token" not in repr(event.payload_json)
    assert "plain-refresh-token" not in repr(event.payload_json)


async def test_authorization_status_exposes_refresh_time_for_qr_polling(monkeypatch) -> None:
    now = utc_now()
    credential = SimpleNamespace(
        authorization_status="active",
        display_name="知识库管理员",
        access_token_expires_at=now + timedelta(hours=2),
        refresh_token_expires_at=now + timedelta(days=30),
        last_refreshed_at=now,
        last_error=None,
    )
    service = FeishuUserOAuthService(db=FakeSession(), environ=oauth_env())
    monkeypatch.setattr(
        service,
        "_get_source",
        AsyncMock(return_value=SimpleNamespace(source_id="source-1")),
    )
    monkeypatch.setattr(service, "_get_credential", AsyncMock(return_value=credential))

    status = await service.get_authorization_status("source-1")

    assert status["authorized"] is True
    assert status["last_refreshed_at"] == now.isoformat()


async def test_expired_access_token_is_refreshed_and_rotated_refresh_token_is_encrypted(monkeypatch) -> None:
    environment = oauth_env()
    cipher = FeishuTokenCipher.from_environ(environment)
    now = utc_now()
    credential = FeishuUserOAuthCredential(
        source_id="source-1",
        access_token_ciphertext=cipher.encrypt("expired-access-token"),
        refresh_token_ciphertext=cipher.encrypt("old-refresh-token"),
        access_token_expires_at=now - timedelta(seconds=1),
        refresh_token_expires_at=now + timedelta(days=20),
        authorization_status="active",
    )
    session = FakeSession()
    service = FeishuUserOAuthService(db=session, environ=environment)
    monkeypatch.setattr(service, "_get_credential", AsyncMock(return_value=credential))
    monkeypatch.setattr(
        service,
        "_refresh_tokens",
        AsyncMock(
            return_value={
                "access_token": "new-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 7200,
                "refresh_token_expires_in": 2_592_000,
            }
        ),
    )

    access_token = await service.get_access_token("source-1")

    assert access_token == "new-access-token"
    assert cipher.decrypt(credential.access_token_ciphertext) == "new-access-token"
    assert cipher.decrypt(credential.refresh_token_ciphertext) == "rotated-refresh-token"
    assert credential.last_error is None
    assert session.flush_count == 1


async def test_missing_credential_requires_user_authorization(monkeypatch) -> None:
    service = FeishuUserOAuthService(db=FakeSession(), environ=oauth_env())
    monkeypatch.setattr(service, "_get_credential", AsyncMock(return_value=None))

    with pytest.raises(FeishuUserOAuthError) as raised:
        await service.get_access_token("source-1")

    assert raised.value.code == "FEISHU_USER_AUTHORIZATION_REQUIRED"
    assert raised.value.status_code == 424


async def test_inactive_credential_requires_user_reauthorization(monkeypatch) -> None:
    credential = SimpleNamespace(authorization_status="reauthorization_required")
    service = FeishuUserOAuthService(db=FakeSession(), environ=oauth_env())
    monkeypatch.setattr(service, "_get_credential", AsyncMock(return_value=credential))

    with pytest.raises(FeishuUserOAuthError) as raised:
        await service.get_access_token("source-1")

    assert raised.value.code == "FEISHU_USER_REAUTHORIZATION_REQUIRED"
    assert raised.value.status_code == 424
    assert str(raised.value) == "飞书用户授权已失效，请重新授权"
