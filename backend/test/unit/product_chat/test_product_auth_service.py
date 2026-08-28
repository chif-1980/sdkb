from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.integrations.feishu import FeishuPermissionError
from yuxi.product_chat.auth_service import (
    SESSION_TTL_SECONDS,
    ProductAuthError,
    ProductAuthService,
)
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_product import (
    AuthorizationStatus,
    FeishuDepartmentBinding,
    FeishuUserBinding,
    FeishuUserDepartmentMembership,
)
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


class FakeDirectoryClient:
    def __init__(self, *, employee: dict | None = None, departments: dict[str, str] | None = None):
        self.employee = employee or {
            "user_id": "employee-001",
            "open_id": "ou_employee",
            "name": "Employee",
            "department_ids": ["od_engineering", "od_product"],
            "status": {
                "is_activated": True,
                "is_frozen": False,
                "is_resigned": False,
                "is_exited": False,
                "is_unjoin": False,
            },
        }
        self.departments = departments or {
            "od_engineering": "Engineering",
            "od_product": "Product",
        }

    async def get_employee(self, user_id: str) -> dict:
        assert user_id == "employee-001"
        return self.employee

    async def get_department(self, department_id: str) -> dict:
        return {"department_id": department_id, "name": self.departments[department_id]}


class FailingDirectoryClient:
    async def get_employee(self, user_id: str) -> dict:
        raise FeishuPermissionError("directory permission missing")


class OpenDepartmentIdDirectoryClient(FakeDirectoryClient):
    async def get_department(self, department_id: str) -> dict:
        return {
            "department_id": f"legacy-{department_id}",
            "open_department_id": department_id,
            "name": self.departments[department_id],
        }


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
    directory_client: FakeDirectoryClient | FailingDirectoryClient | None = None,
) -> ProductAuthService:
    client = httpx.AsyncClient(transport=_feishu_transport(profile, top_level_token=top_level_token))
    return ProductAuthService(
        db=db_session,
        redis_client=redis,
        http_client=client,
        directory_client=directory_client or FakeDirectoryClient(),
    )


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


async def test_first_login_automatically_provisions_employee(db_session):
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_login_url())

    user, _ = await service.complete_callback(code="oauth-code", state=state)
    binding = await db_session.scalar(select(FeishuUserBinding).where(FeishuUserBinding.user_id == user.id))
    memberships = list(
        (
            await db_session.scalars(
                select(FeishuUserDepartmentMembership).where(FeishuUserDepartmentMembership.user_id == user.id)
            )
        ).all()
    )

    assert user.uid == "employee-001"
    assert user.username == "Employee"
    assert user.role == "user"
    assert user.department_id is not None
    assert user.password_hash.startswith("$argon2")
    assert binding is not None
    assert binding.feishu_open_id == "ou_employee"
    assert binding.authorization_status == AuthorizationStatus.ACTIVE
    assert len(memberships) == 2


async def test_first_login_accepts_feishu_root_department_without_a_name(db_session):
    employee = FakeDirectoryClient().employee | {"department_ids": ["0", "od_product"]}
    service = _service(
        db_session,
        FakeRedis(),
        directory_client=FakeDirectoryClient(
            employee=employee,
            departments={"0": "", "od_product": "Product"},
        ),
    )

    user = await service.resolve_bound_user(_profile())
    department_bindings = list((await db_session.scalars(select(FeishuDepartmentBinding))).all())
    product_binding = next(item for item in department_bindings if item.feishu_department_id == "od_product")

    assert user.department_id == product_binding.department_id
    assert {(item.feishu_department_id, item.display_name) for item in department_bindings} == {
        ("0", "企业根部门"),
        ("od_product", "Product"),
    }


async def test_first_login_validates_open_department_id_when_both_id_types_are_returned(db_session):
    service = _service(
        db_session,
        FakeRedis(),
        directory_client=OpenDepartmentIdDirectoryClient(),
    )

    user = await service.resolve_bound_user(_profile())

    assert user.department_id is not None


async def test_first_binding_requires_matching_uid_and_department(db_session):
    redis = FakeRedis()
    service = _service(db_session, redis)
    state = _state_from_url(await service.create_login_url())

    resolved_user, _ = await service.complete_callback(code="oauth-code", state=state)
    binding = await db_session.get(FeishuUserBinding, 1)

    assert resolved_user.uid == "employee-001"
    assert binding is not None
    assert binding.user_id == resolved_user.id
    assert binding.feishu_open_id == "ou_employee"
    assert binding.tenant_key == "tenant-a"
    assert binding.authorization_status == AuthorizationStatus.ACTIVE


async def test_callback_accepts_oauth_v2_top_level_access_token(db_session):
    redis = FakeRedis()
    service = _service(db_session, redis, top_level_token=True)
    state = _state_from_url(await service.create_login_url())

    resolved_user, _ = await service.complete_callback(code="oauth-code", state=state)

    assert resolved_user.uid == "employee-001"


@pytest.mark.parametrize(
    ("profile_overrides", "employee_overrides"),
    [
        ({"user_id": None}, {}),
        ({"tenant_key": None}, {}),
        ({}, {"user_id": "someone-else"}),
        ({}, {"open_id": "ou_someone_else"}),
        ({}, {"department_ids": []}),
        ({}, {"status": {"is_activated": False}}),
        ({}, {"status": {"is_activated": True, "is_resigned": True}}),
    ],
)
async def test_first_binding_rejects_untrusted_identity(db_session, profile_overrides, employee_overrides):
    employee = FakeDirectoryClient().employee | employee_overrides
    service = _service(
        db_session,
        FakeRedis(),
        _profile(**profile_overrides),
        directory_client=FakeDirectoryClient(employee=employee),
    )

    with pytest.raises(ProductAuthError) as exc_info:
        await service.resolve_bound_user(_profile(**profile_overrides))

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "IDENTITY_MAPPING_REQUIRED"


async def test_directory_permission_failure_returns_actionable_stable_error(db_session):
    service = _service(
        db_session,
        FakeRedis(),
        directory_client=FailingDirectoryClient(),
    )

    with pytest.raises(ProductAuthError) as exc_info:
        await service.resolve_bound_user(_profile())

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "FEISHU_DIRECTORY_UNAVAILABLE"


@pytest.mark.parametrize("failure", ["revoked", "tenant", "deleted"])
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
    await db_session.commit()

    service = _service(db_session, FakeRedis(), profile)
    with pytest.raises(ProductAuthError) as exc_info:
        await service.resolve_bound_user(profile)

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "IDENTITY_MAPPING_REQUIRED"


async def test_existing_binding_refreshes_profile_and_all_departments(db_session):
    user = await _add_user(db_session)
    binding = FeishuUserBinding(
        user_id=user.id,
        feishu_open_id="ou_employee",
        feishu_user_id="employee-001",
        feishu_union_id="on_employee",
        tenant_key="tenant-a",
        display_name="Old name",
        authorization_status=AuthorizationStatus.ACTIVE,
    )
    db_session.add(binding)
    await db_session.commit()

    employee = FakeDirectoryClient().employee | {"name": "Updated Employee"}
    service = _service(
        db_session,
        FakeRedis(),
        directory_client=FakeDirectoryClient(employee=employee),
    )
    resolved = await service.resolve_bound_user(_profile(name="Updated Employee"))
    memberships = list(
        (
            await db_session.scalars(
                select(FeishuUserDepartmentMembership).where(FeishuUserDepartmentMembership.user_id == user.id)
            )
        ).all()
    )
    department_bindings = list((await db_session.scalars(select(FeishuDepartmentBinding))).all())

    assert resolved.id == user.id
    assert resolved.username == "Updated Employee"
    assert resolved.avatar == "https://img.example/avatar.png"
    assert resolved.department_id is not None
    assert {item.feishu_department_id for item in department_bindings} == {
        "od_engineering",
        "od_product",
    }
    assert len(memberships) == 2


async def test_repeated_login_reuses_the_same_user_and_binding(db_session):
    service = _service(db_session, FakeRedis())

    first = await service.resolve_bound_user(_profile())
    second = await service.resolve_bound_user(_profile())

    assert second.id == first.id
    assert len(list((await db_session.scalars(select(User))).all())) == 1
    assert len(list((await db_session.scalars(select(FeishuUserBinding))).all())) == 1


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
