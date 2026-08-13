from __future__ import annotations

import httpx
import pytest

import yuxi.integrations.feishu.client as feishu_client_module
from yuxi.integrations.feishu import (
    FeishuAuthenticationError,
    FeishuApiError,
    FeishuClient,
    FeishuCredentialError,
    FeishuPermissionError,
)


def _client(handler, **kwargs) -> FeishuClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://open.feishu.test")
    return FeishuClient(client=http_client, environ={"FEISHU_ACCESS_TOKEN": "test-token"}, **kwargs)


@pytest.mark.asyncio
async def test_injected_client_does_not_require_a_base_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "open.feishu.cn"
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ={"FEISHU_ACCESS_TOKEN": "test-token"})

    node = await client.get_node("page-token")

    assert node.node_token == "page-token"
    await client.aclose()


@pytest.mark.asyncio
async def test_aclose_does_not_close_an_injected_client() -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    client = FeishuClient(client=http_client, environ={"FEISHU_ACCESS_TOKEN": "test-token"})

    await client.aclose()

    assert http_client.is_closed is False
    await http_client.aclose()


@pytest.mark.asyncio
async def test_get_node_returns_page_details() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/open-apis/wiki/v2/spaces/get_node"
        assert request.url.params["token"] == "page-token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "node": {
                        "space_id": "space-1",
                        "node_token": "page-token",
                        "obj_token": "doc-token",
                        "obj_type": "docx",
                        "title": "Page title",
                        "has_child": True,
                        "obj_edit_time": "1710000000",
                    }
                },
            },
        )

    client = _client(handler)
    node = await client.get_node("page-token")

    assert node.space_id == "space-1"
    assert node.node_token == "page-token"
    assert node.title == "Page title"
    assert node.has_child is True
    await client.aclose()


@pytest.mark.asyncio
async def test_list_children_follows_page_token() -> None:
    requested_tokens: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path.endswith("/get_node"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"node": {"space_id": "space-1", "node_token": "root-token"}}},
            )

        assert request.url.path == "/open-apis/wiki/v2/spaces/space-1/nodes"
        page_token = request.url.params.get("page_token")
        requested_tokens.append(page_token)
        if page_token is None:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"space_id": "space-1", "node_token": "child-1", "title": "One"}],
                        "has_more": True,
                        "page_token": "next-page",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [{"space_id": "space-1", "node_token": "child-2", "title": "Two"}],
                    "has_more": False,
                },
            },
        )

    client = _client(handler)
    nodes = await client.list_children("root-token")

    assert [node.node_token for node in nodes] == ["child-1", "child-2"]
    assert requested_tokens == [None, "next-page"]
    await client.aclose()


@pytest.mark.asyncio
async def test_list_attachments_follows_page_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/open-apis/drive/v1/files"
        if request.url.params.get("page_token") is None:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "files": [{"token": "file-1", "name": "one.pdf", "type": "file", "size": "12"}],
                        "has_more": True,
                        "page_token": "next-page",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "files": [{"token": "file-2", "name": "two.txt", "type": "file", "size": "4"}],
                    "has_more": False,
                },
            },
        )

    client = _client(handler)
    attachments = await client.list_attachments("folder-token")

    assert [(item.file_token, item.name, item.size) for item in attachments] == [
        ("file-1", "one.pdf", 12),
        ("file-2", "two.txt", 4),
    ]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["list_children", "list_attachments"])
@pytest.mark.parametrize("page_token", [None, "", 123])
async def test_paginated_response_missing_or_invalid_page_token_raises(entrypoint: str, page_token: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_node"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"node": {"space_id": "space-1", "node_token": "root-token"}}},
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"items": [], "files": [], "has_more": True, "page_token": page_token}},
        )

    client = _client(handler)
    with pytest.raises(FeishuApiError, match="page token"):
        await getattr(client, entrypoint)("root-token")

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["list_children", "list_attachments"])
async def test_paginated_response_reusing_page_token_raises(entrypoint: str) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        if request.url.path.endswith("/get_node"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"node": {"space_id": "space-1", "node_token": "root-token"}}},
            )
        requests += 1
        assert requests < 3, "client requested a repeated page token indefinitely"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"items": [], "files": [], "has_more": True, "page_token": "repeat"}},
        )

    client = _client(handler)
    with pytest.raises(FeishuApiError, match="repeated page token"):
        await getattr(client, entrypoint)("root-token")

    await client.aclose()


@pytest.mark.asyncio
async def test_download_returns_binary_content_and_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/open-apis/drive/v1/files/file-1/download"
        return httpx.Response(
            200,
            content=b"file bytes",
            headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="one.pdf"'},
        )

    client = _client(handler)
    download = await client.download("file-1")

    assert download.file_token == "file-1"
    assert download.content == b"file bytes"
    assert download.content_type == "application/pdf"
    assert download.file_name == "one.pdf"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(401, FeishuAuthenticationError), (403, FeishuPermissionError)],
)
async def test_auth_errors_are_normalized(status_code: int, error_type: type[Exception]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers={"x-tt-logid": "request-1"}, json={"token": "do-not-log"})

    client = _client(handler)
    with pytest.raises(error_type) as raised:
        await client.get_node("page-token")

    assert raised.value.status_code == status_code
    assert raised.value.request_id == "request-1"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_field", ["msg", "message"])
async def test_business_error_uses_feishu_message(error_field: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 999, error_field: "Readable Feishu error", "token": "response-secret"},
        )

    client = _client(handler)
    with pytest.raises(FeishuApiError, match="Readable Feishu error") as raised:
        await client.get_node("page-token")

    assert "response-secret" not in str(raised.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_response_logging_excludes_tokens_and_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    log_messages: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-tt-logid": "request-1"},
            json={"code": 0, "data": {"node": {"node_token": "page-token"}, "token": "response-secret"}},
        )

    def capture_log(message: str, *args: object) -> None:
        log_messages.append(message.format(*args))

    monkeypatch.setattr(feishu_client_module.logger, "info", capture_log)
    client = _client(handler)
    await client.get_node("page-token")

    assert log_messages == ["Feishu response: status=200, request_id=request-1"]
    assert "test-token" not in " ".join(log_messages)
    assert "response-secret" not in " ".join(log_messages)
    await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_retries_after_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0.25"})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = _client(handler, sleep=fake_sleep)
    node = await client.get_node("page-token")

    assert node.node_token == "page-token"
    assert attempts == 2
    assert delays == [0.25]
    await client.aclose()


@pytest.mark.asyncio
async def test_server_error_retries_with_bounded_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, headers={"x-request-id": "request-1"})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = _client(handler, sleep=fake_sleep)
    await client.get_node("page-token")

    assert attempts == 3
    assert delays == [1.0, 2.0]
    await client.aclose()


@pytest.mark.asyncio
async def test_any_server_error_retries_with_bounded_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(507, headers={"x-request-id": "request-1"})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = _client(handler, sleep=fake_sleep)
    await client.get_node("page-token")

    assert attempts == 2
    assert delays == [1.0]
    await client.aclose()


def test_missing_credential_is_explicit() -> None:
    with pytest.raises(FeishuCredentialError, match="FEISHU_ACCESS_TOKEN"):
        FeishuClient(credential_env_name="FEISHU_ACCESS_TOKEN", environ={})
