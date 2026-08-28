from __future__ import annotations

import io
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.utils.access_log_middleware import AccessLogMiddleware


pytestmark = pytest.mark.unit


def test_access_log_ignores_query_values_and_invalid_forwarded_ip():
    output = io.StringIO()
    logger = logging.Logger("access-log-security-test")
    logger.addHandler(logging.StreamHandler(output))

    app = FastAPI()

    @app.get("/probe")
    async def probe():
        return {"status": "ok"}

    app.add_middleware(AccessLogMiddleware, logger=logger)
    response = TestClient(app).get(
        "/probe?token=private-value",
        headers={"X-Forwarded-For": "'\r\nforged-log-entry"},
    )

    assert response.status_code == 200
    logged = output.getvalue()
    assert "GET /probe HTTP/" in logged
    assert "private-value" not in logged
    assert "forged-log-entry" not in logged
