"""Opt-in live regression for the architecture / architecture-diagram incident."""
import json
import os
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


@pytest.mark.parametrize("question", ["投标机器人系统架构图", "投标机器人系统架构"])
async def test_architecture_requests_return_cited_answers(question):
    token = os.getenv("PRODUCT_E2E_SESSION_COOKIE", "")
    if os.getenv("RUN_ARCHITECTURE_E2E") != "1" or not token:
        pytest.skip("Requires explicit live knowledge/model E2E configuration")
    async with httpx.AsyncClient(
        base_url=os.getenv("TEST_BASE_URL", "http://localhost:5050"),
        cookies={"enterprise_assistant_session": token}, timeout=240,
    ) as client:
        created = await client.post("/api/chat/conversations", json={})
        assert created.status_code == 201
        cid = created.json()["conversation"]["id"]
        try:
            response = await client.post(
                f"/api/chat/conversations/{cid}/messages/stream",
                json={"content": question, "mode": "DETAILED"},
            )
            assert response.status_code == 200
            events = []
            for block in response.text.split("\n\n"):
                lines = block.splitlines()
                kind = next((line[7:] for line in lines if line.startswith("event: ")), "")
                data = next((line[6:] for line in lines if line.startswith("data: ")), "")
                if data:
                    events.append((kind, json.loads(data)))
            assert not [data for kind, data in events if kind == "error"]
            result = next(data for kind, data in events if kind == "complete")
            answer = result["assistantMessage"]
            assert answer["answerStatus"] == "SUPPORTED"
            assert answer["citations"]
            if question.endswith("图"):
                assert "```mermaid" in answer["content"]
                assert "示意图" in answer["content"]
                assert "非原始" in answer["content"]
            citation = await client.get(f"/api/citations/{answer['citations'][0]['id']}")
            assert citation.status_code == 200
            # Local opt-in evidence artifact, without credentials, for visual QA.
            output = os.getenv("ARCHITECTURE_E2E_OUTPUT")
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                Path(output, f"{cid}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            print(f"verified conversation={cid} question={question} citations={len(answer['citations'])}")
        finally:
            archived = await client.post(f"/api/chat/conversations/{cid}/archive")
            assert archived.is_success
