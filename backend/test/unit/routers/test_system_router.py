from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers.system_router import system
from server.utils.auth_middleware import get_admin_user
from yuxi.utils import logging_config

pytestmark = pytest.mark.unit


def test_discovery_endpoint_is_public(monkeypatch):
    monkeypatch.setattr("server.routers.system_router.get_version", lambda: "0.7.1.dev0")

    app = FastAPI()
    app.include_router(system, prefix="/api")
    response = TestClient(app).get("/api/system/discovery")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "Yuxi"
    assert payload["version"] == "0.7.1.dev0"
    assert payload["api_prefix"] == "/api"
    assert payload["capabilities"]["cli"]["browser_login"] is True
    assert payload["capabilities"]["cli"]["api_key_auth"] is True
    assert payload["capabilities"]["cli"]["kb_upload"] is True
    assert payload["endpoints"]["cli_auth_sessions"] == "/api/auth/cli/sessions"


def test_logs_return_generic_error_without_internal_exception(monkeypatch):
    app = FastAPI()
    app.include_router(system, prefix="/api")
    app.dependency_overrides[get_admin_user] = lambda: object()

    def fail_to_open(*_args, **_kwargs):
        raise OSError("postgresql connection failed at /srv/private/database")

    monkeypatch.setattr("server.routers.system_router.aiofiles.open", fail_to_open)
    response = TestClient(app, raise_server_exceptions=False).get("/api/system/logs?levels=ERROR")

    assert response.status_code == 500
    assert response.json() == {"detail": "获取系统日志失败"}
    assert "postgresql" not in response.text
    assert "/srv/private" not in response.text


def test_logs_exclude_log_file_path(monkeypatch, tmp_path):
    log_file = tmp_path / "yuxi.log"
    log_file.write_text("2026-08-27 10:00:00 - INFO - main.py:1 - ready\n", encoding="utf-8")
    monkeypatch.setattr(logging_config, "LOG_FILE", str(log_file))

    app = FastAPI()
    app.include_router(system, prefix="/api")
    app.dependency_overrides[get_admin_user] = lambda: object()
    response = TestClient(app).get("/api/system/logs?levels=INFO")

    assert response.status_code == 200
    assert response.json() == {
        "log": "2026-08-27 10:00:00 - INFO - main.py:1 - ready\n",
        "message": "success",
    }


def test_logs_redact_warning_and_error_details(monkeypatch, tmp_path):
    log_file = tmp_path / "yuxi.log"
    log_file.write_text(
        "2026-08-27 10:00:00 - WARNING - db.py:10 - postgresql connection at /srv/private/db failed\n"
        "2026-08-27 10:00:01 - ERROR - auth.py:20 - secret-token leaked\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(logging_config, "LOG_FILE", str(log_file))

    app = FastAPI()
    app.include_router(system, prefix="/api")
    app.dependency_overrides[get_admin_user] = lambda: object()
    response = TestClient(app).get("/api/system/logs?levels=WARNING,ERROR")

    assert response.status_code == 200
    assert response.json()["log"] == (
        "2026-08-27 10:00:00 - WARNING - db.py:10 - [details redacted]\n"
        "2026-08-27 10:00:01 - ERROR - auth.py:20 - [details redacted]\n"
    )
    assert "postgresql" not in response.text
    assert "/srv/private" not in response.text
    assert "secret-token" not in response.text
