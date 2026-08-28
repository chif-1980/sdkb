from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers.auth_router import auth
from server.routers.chat_router import chat
from server.utils.auth_middleware import get_admin_user, get_db, get_required_user


pytestmark = pytest.mark.unit


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(auth, prefix="/api")
    app.include_router(chat, prefix="/api")
    user = SimpleNamespace(uid="test-user", role="superadmin", department_id=1)
    app.dependency_overrides[get_admin_user] = lambda: user
    app.dependency_overrides[get_required_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/users?limit=25000",
        "/api/auth/users?limit=99999999999999999999",
        "/api/auth/users?skip=99999999999999999999",
        "/api/chat/threads?offset=99999999999999999999",
        "/api/chat/threads/search?q=test&offset=99999999999999999999",
    ],
)
def test_scanner_pagination_values_are_rejected_before_handler(client, path):
    response = client.get(path)

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] in {"less_than_equal", "int_parsing", "int_type"}
