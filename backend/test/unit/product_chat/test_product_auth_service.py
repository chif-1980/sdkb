from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.product_chat.auth_service import (
    SESSION_TTL_SECONDS,
    ProductAuthError,
    ProductAuthService,
)
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_product import AuthorizationStatus, FeishuUserBinding
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        self.ttl[key] = ex
        return True

    async def getdel(self, key: str) -> str | None:
        return self.data.pop(key, None)


def _profile(**overrides) -> dict:
    profile = {
        "open_id": "ou_employee",
        "user_id": "employee-001",
        "union_id": "on_employee",
        "tenant_key": "tenant-a",
        "name": "Employee",
        "avatar_url": "https://img.example/avatar.png",
    }
    profile.update(overrides)
    return profile


def _feishu_transport(profile: dict | None = None, *, top_level_token: bool = False) -> httpx.MockTransport:
    user_profile = profile or _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/authen/v2/oauth/token":
            body = json.loads(request.content)
            assert body["grant_type"] == "authorization_code"
            assert body["client_id"] == "test-app-id"
            assert body["client_secret"] == "test-app-secret"
            assert body["redirect_uri"] == "https://assistant.example/api/auth/feishu/callback"
            if top_level_token:
                return httpx.Response(200, json={"code": 0, "access_token": "user-access-token"})
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "user-access-token"}})
        if request.url.path == "/open-apis/authen/v1/user_info":
            assert request.headers["authorization"] == "Bearer user-access-token"
            return httpx.Response(200, json={"code": 0, "data": user_profile})
        raise AssertionError(f"unexpected Feishu request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _state_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEISHU_APP_ID", "test-app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("FEISHU_PRODUCT_REDIRECT_URI", "https://assistant.example/api/auth/feishu/callback")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-that-is-long-and-stable")
    monkeypatch.setenv("YUXI_INSTANCE_ID", "product-auth-test")


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _add_user(db_session, *, uid: str = "employee-001", department: bool = True) -> User:
    user = User(
        username="Employee",
        uid=uid,
        password_hash="not-used-by-product-auth",
        role="user",
    )
    if department:
        user.department = Department(name=f"Department {uid}")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _service(
    db_session,
    redis: FakeRedis,
    profile: dict | None = None,
    *,
    top_level_token: bool = False,
) -> ProductAuthService:
    client = httpx.AsyncClient(transport=_feishu_transport(profile, top_level_token=top_level_token))
    return ProductAuthService(db=db_session, redis_client=redis, http_client=client)


@pytest.mark.parametrize("invalid_state", [None, "", "not-a-issued-state"])
async def test_callback_rejects_missing_or_mismatched_state_with_stable_error(db_session, invalid_state):
    service = _service(db_session, FakeRedis())

    with pytest.raises(ProductAuthError) as exc_info:
        await service.complete_callback(code="oauth-code", state=invalid_state)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "FEISHU_OAUTH_STATE_INVALID"


async def test_callback_rejects_expired_state_with_stable_error(db_session):
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_login_url())
    redis.data.clear()

    with pytest.raises(ProductAuthError) as exc_info:
        await service.complete_callback(code="oauth-code", state=state)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "FEISHU_OAUTH_STATE_INVALID"


async def test_callback_consumes_state_once(db_session):
    await _add_user(db_session)
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_login_url())

    user, token = await service.complete_callback(code="oauth-code", state=state)

    assert user.uid == "employee-001"
    payload = AuthUtils.verify_access_token(token)
    assert payload["sub"] == str(user.id)
    assert payload["token_kind"] == "enterprise_assistant"
    assert SESSION_TTL_SECONDS - 2 <= payload["exp"] - int(time.time()) <= SESSION_TTL_SECONDS

    with pytest.raises(ProductAuthError) as exc_info:
        await service.complete_callback(code="oauth-code", state=state)
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "FEISHU_OAUTH_STATE_INVALID"


async def test_valid_state_without_identity_mapping_is_forbidden(db_session):
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_login_url())

    with pytest.raises(ProductAuthError) as exc_info:
        await service.complete_callback(code="oauth-code", state=state)

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "IDENTITY_MAPPING_REQUIRED"


async def test_first_binding_requires_matching_uid_and_department(db_session):
    user = await _add_user(db_session)
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_login_url())

    resolved_user, _ = await service.complete_callback(code="oauth-code", state=state)
    binding = await db_session.get(FeishuUserBinding, 1)

    assert resolved_user.id == user.id
    assert binding is not None
    assert binding.user_id == user.id
    assert binding.feishu_open_id == "ou_employee"
    assert binding.tenant_key == "tenant-a"
    assert binding.authorization_status == AuthorizationStatus.ACTIVE


async def test_callback_accepts_oauth_v2_top_level_access_token(db_session):
    user = await _add_user(db_session)
    redis = FakeRedis()
    service = _service(db_session, redis, top_level_token=True)
    state = _state_from_url(await service.create_login_url())

    resolved_user, _ = await service.complete_callback(code="oauth-code", state=state)

    assert resolved_user.id == user.id


@pytest.mark.parametrize(
    ("user_kwargs", "profile_overrides"),
    [
        ({"uid": "someone-else"}, {}),
        ({"department": False}, {}),
        ({}, {"user_id": None}),
        ({}, {"tenant_key": None}),
    ],
)
async def test_first_binding_rejects_untrusted_identity(db_session, user_kwargs, profile_overrides):
    await _add_user(db_session, **user_kwargs)
    service = _service(db_session, FakeRedis(), _profile(**profile_overrides))

    with pytest.raises(ProductAuthError) as exc_info:
        await service.resolve_bound_user(_profile(**profile_overrides))

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "IDENTITY_MAPPING_REQUIRED"


@pytest.mark.parametrize("failure", ["revoked", "tenant", "deleted", "department"])
async def test_existing_binding_rejects_revocation_tenant_or_invalid_user(db_session, failure):
    user = await _add_user(db_session)
    binding = FeishuUserBinding(
        user_id=user.id,
        feishu_open_id="ou_employee",
        feishu_user_id="employee-001",
        feishu_union_id="on_employee",
        tenant_key="tenant-a",
        display_name="Employee",
        authorization_status=AuthorizationStatus.ACTIVE,
    )
    db_session.add(binding)
    await db_session.commit()

    profile = _profile()
    if failure == "revoked":
        binding.authorization_status = AuthorizationStatus.REVOKED
    elif failure == "tenant":
        profile["tenant_key"] = "tenant-b"
    elif failure == "deleted":
        user.is_deleted = 1
    else:
        user.department_id = None
    await db_session.commit()

    service = _service(db_session, FakeRedis(), profile)
    with pytest.raises(ProductAuthError) as exc_info:
        await service.resolve_bound_user(profile)

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "IDENTITY_MAPPING_REQUIRED"


@pytest.mark.parametrize(
    ("return_path", "expected"),
    [
        ("/chat", "/chat"),
        ("https://evil.example/chat", "/chat"),
        ("//evil.example/chat", "/chat"),
        ("/admin", "/chat"),
        ("", "/chat"),
    ],
)
async def test_login_state_only_stores_hash_normalized_path_and_expiry(db_session, return_path, expected):
    redis = FakeRedis()
    service = _service(db_session, redis)

    login_url = await service.create_login_url(return_path)

    parsed = urlparse(login_url)
    query = parse_qs(parsed.query)
    state = query["state"][0]
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.feishu.cn"
    assert parsed.path == "/open-apis/authen/v1/authorize"
    assert query["app_id"] == ["test-app-id"]
    assert query["redirect_uri"] == ["https://assistant.example/api/auth/feishu/callback"]
    assert "test-app-secret" not in login_url

    assert len(redis.data) == 1
    key, raw_value = next(iter(redis.data.items()))
    stored = json.loads(raw_value)
    assert redis.ttl[key] == 300
    assert state not in key
    assert state not in raw_value
    assert stored["return_path"] == expected
    assert set(stored) == {"state_hash", "return_path", "expires_at"}
    assert stored["state_hash"] in key
    assert "test-app-secret" not in raw_value


async def test_qr_login_uses_embeddable_authorize_url_and_one_time_state(db_session):
    redis = FakeRedis()
    service = _service(db_session, redis)

    login_url = await service.create_qr_login_url()

    parsed = urlparse(login_url)
    query = parse_qs(parsed.query)
    state = query["state"][0]
    assert parsed.scheme == "https"
    assert parsed.netloc == "passport.feishu.cn"
    assert parsed.path == "/suite/passport/oauth/authorize"
    assert query["client_id"] == ["test-app-id"]
    assert query["redirect_uri"] == ["https://assistant.example/api/auth/feishu/callback"]
    assert query["response_type"] == ["code"]
    assert "test-app-secret" not in login_url

    assert len(redis.data) == 1
    key, raw_value = next(iter(redis.data.items()))
    stored = json.loads(raw_value)
    assert redis.ttl[key] == 300
    assert state not in key
    assert state not in raw_value
    assert set(stored) == {"state_hash", "return_path", "expires_at"}
    assert stored["return_path"] == "/chat"
    assert "test-app-secret" not in raw_value


async def test_qr_login_callback_reuses_secure_product_session_flow(db_session):
    await _add_user(db_session)
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_qr_login_url())

    user, token = await service.complete_callback(code="qr-oauth-code", state=state)

    assert user.uid == "employee-001"
    assert AuthUtils.verify_access_token(token)["token_kind"] == "enterprise_assistant"
    assert not redis.data


async def test_callback_logs_exclude_code_access_token_and_cookie(db_session, caplog):
    await _add_user(db_session)
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_login_url())

    await service.complete_callback(code="secret-oauth-code", state=state)

    assert "secret-oauth-code" not in caplog.text
    assert "user-access-token" not in caplog.text
    assert "enterprise_assistant_session" not in caplog.text
