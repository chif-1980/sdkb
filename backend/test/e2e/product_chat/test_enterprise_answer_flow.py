from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

COOKIE_NAME = "enterprise_assistant_session"


def _real_e2e_settings() -> dict[str, str]:
    if os.getenv("RUN_REAL_PRODUCT_E2E") != "1":
        pytest.skip("Real enterprise assistant E2E is disabled.")

    names = (
        "PRODUCT_E2E_SESSION_COOKIE",
        "PRODUCT_E2E_SUPPORTED_QUESTION",
        "PRODUCT_E2E_INSUFFICIENT_QUESTION",
        "PRODUCT_E2E_CONFLICTING_QUESTION",
    )
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.fail(f"Real enterprise assistant E2E configuration is missing: {', '.join(missing)}")
    return values


async def _ask(
    client: httpx.AsyncClient,
    question: str,
) -> tuple[str, dict]:
    create_response = await client.post("/api/chat/conversations", json={})
    assert create_response.status_code == 201
    conversation_id = create_response.json()["conversation"]["id"]

    answer_response = await client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        json={"content": question},
    )
    assert answer_response.status_code == 201
    return conversation_id, answer_response.json()["assistantMessage"]


async def test_real_enterprise_answers_cover_grounding_and_citation_access(
    e2e_base_url: str,
):
    settings = _real_e2e_settings()
    conversation_ids: list[str] = []

    async with httpx.AsyncClient(
        base_url=e2e_base_url,
        cookies={COOKIE_NAME: settings["PRODUCT_E2E_SESSION_COOKIE"]},
        follow_redirects=False,
        timeout=httpx.Timeout(300.0, connect=10.0),
    ) as client:
        session_response = await client.get("/api/session")
        assert session_response.status_code == 200

        try:
            conversation_id, supported = await _ask(
                client,
                settings["PRODUCT_E2E_SUPPORTED_QUESTION"],
            )
            conversation_ids.append(conversation_id)
            assert supported["answerStatus"] == "SUPPORTED"
            assert supported["citations"]

            citation_id = supported["citations"][0]["id"]
            citation_response = await client.get(f"/api/citations/{citation_id}")
            assert citation_response.status_code == 200
            assert citation_response.json()["id"] == citation_id

            open_response = await client.get(f"/api/citations/{citation_id}/open")
            assert open_response.status_code == 307
            source_url = urlparse(open_response.headers.get("location", ""))
            if source_url.scheme != "https" or not (source_url.hostname or "").endswith(".feishu.cn"):
                pytest.fail("The supported answer citation did not resolve to an HTTPS Feishu page.")

            conversation_id, insufficient = await _ask(
                client,
                settings["PRODUCT_E2E_INSUFFICIENT_QUESTION"],
            )
            conversation_ids.append(conversation_id)
            assert insufficient["answerStatus"] == "INSUFFICIENT"
            assert insufficient["content"] == "暂无足够可靠资料"
            assert insufficient["citations"] == []

            conversation_id, conflicting = await _ask(
                client,
                settings["PRODUCT_E2E_CONFLICTING_QUESTION"],
            )
            conversation_ids.append(conversation_id)
            assert conflicting["answerStatus"] == "CONFLICTING"
        finally:
            for conversation_id in conversation_ids:
                await client.post(f"/api/chat/conversations/{conversation_id}/archive")
