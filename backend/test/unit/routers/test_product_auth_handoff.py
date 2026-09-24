from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.product_auth_router import (
    _ASSISTANT_HANDOFF_PREFIX,
    complete_assistant_handoff,
    create_assistant_handoff,
)
from yuxi.product_chat.auth_service import ProductAuthService
from yuxi.storage.postgres.models_business import Base, Department, User

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, *, ex, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def getdel(self, key):
        return self.data.pop(key, None)

    async def delete(self, key):
        self.data.pop(key, None)


@pytest_asyncio.fixture
async def handoff_context(monkeypatch):
    monkeypatch.setenv("YUXI_ENV", "development")
    monkeypatch.setenv("FEISHU_PRODUCT_REDIRECT_URI", "http://127.0.0.1:5174/api/auth/feishu/callback")
    monkeypatch.setenv("JWT_SECRET_KEY", "handoff-test-secret-that-is-long-and-stable")
    monkeypatch.setenv("YUXI_INSTANCE_ID", "handoff-test")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        department = Department(name="企业部门")
        user = User(username="管理员", uid="admin-1", role="admin", password_hash="unused", department=department)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        redis = FakeRedis()
        yield db, user, redis, ProductAuthService(db=db, redis_client=redis)
    await engine.dispose()


async def test_manager_handoff_is_one_time_and_sets_assistant_cookie(handoff_context):
    db, user, redis, service = handoff_context

    result = await create_assistant_handoff(current_user=user, service=service)
    callback_url = result["url"]
    code = parse_qs(urlparse(callback_url).query)["code"][0]
    assert urlparse(callback_url).path == "/api/auth/assistant/callback"
    assert _ASSISTANT_HANDOFF_PREFIX + code in redis.data

    response = await complete_assistant_handoff(code=code, service=service)
    assert response.status_code == 303
    assert response.headers["location"] == "http://127.0.0.1:5174/chat"
    assert "enterprise_assistant_session=" in response.headers["set-cookie"]
    assert _ASSISTANT_HANDOFF_PREFIX + code not in redis.data

    expired = await complete_assistant_handoff(code=code, service=service)
    assert expired.status_code == 303
    assert "error=SSO_EXPIRED" in expired.headers["location"]
