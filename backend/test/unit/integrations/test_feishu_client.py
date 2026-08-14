from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import yuxi.integrations.feishu.client as feishu_client_module
from yuxi.integrations.feishu import (
    FeishuAuthenticationError,
    FeishuApiError,
    FeishuClient,
    FeishuCredentialError,
    FeishuNotFoundError,
    FeishuPermissionError,
)
from yuxi.integrations.feishu.schemas import FeishuNode

APP_ENV = {"FEISHU_APP_ID": "test-app-id", "FEISHU_APP_SECRET": "test-app-secret"}
TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"


def _client(handler, **kwargs) -> FeishuClient:
    async def authenticated_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TENANT_TOKEN_PATH:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-tenant-token", "expire": 7200})
        response = handler(request)
        return await response if hasattr(response, "__await__") else response

    transport = httpx.MockTransport(authenticated_handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://open.feishu.test")
    return FeishuClient(client=http_client, environ=APP_ENV, **kwargs)


def test_app_credential_environment_names_are_exported() -> None:
    assert feishu_client_module.DEFAULT_APP_ID_ENV_NAME == "FEISHU_APP_ID"
    assert feishu_client_module.DEFAULT_APP_SECRET_ENV_NAME == "FEISHU_APP_SECRET"
    assert not hasattr(feishu_client_module, "DEFAULT_CREDENTIAL_ENV_NAME")


@pytest.mark.parametrize(
    ("environ", "missing_name"),
    [
        ({"FEISHU_APP_SECRET": "secret-value"}, "FEISHU_APP_ID"),
        ({"FEISHU_APP_ID": "app-value"}, "FEISHU_APP_SECRET"),
        ({"FEISHU_APP_ID": "  ", "FEISHU_APP_SECRET": "secret-value"}, "FEISHU_APP_ID"),
        ({"FEISHU_APP_ID": "app-value", "FEISHU_APP_SECRET": "\t"}, "FEISHU_APP_SECRET"),
    ],
)
def test_missing_or_blank_app_credential_is_explicit(environ: dict[str, str], missing_name: str) -> None:
    with pytest.raises(FeishuCredentialError) as raised:
        FeishuClient(environ=environ)

    assert str(raised.value) == f"Missing Feishu credential environment variable: {missing_name}"
    assert "app-value" not in str(raised.value)
    assert "secret-value" not in str(raised.value)


@pytest.mark.asyncio
async def test_first_business_request_exchanges_app_credentials_for_tenant_token() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TENANT_TOKEN_PATH:
            assert request.method == "POST"
            assert "authorization" not in request.headers
            assert request.headers["content-type"] == "application/json"
            assert json.loads(request.content) == {"app_id": "test-app-id", "app_secret": "test-app-secret"}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer tenant-token"
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer inherited-token"}
    )
    client = FeishuClient(client=http_client, environ=APP_ENV)

    node = await client.get_node("page-token")

    assert node.node_token == "page-token"
    assert [request.url.path for request in requests] == [TENANT_TOKEN_PATH, "/open-apis/wiki/v2/spaces/get_node"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_tenant_token_is_reused_while_fresh() -> None:
    auth_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    await client.get_node("page-token")
    await client.get_node("page-token")

    assert auth_attempts == 1
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expire", "before_refresh", "at_refresh"),
    [(7200, 6899.0, 6900.0), (100, 49.0, 50.0)],
)
async def test_tenant_token_refreshes_at_expiry_margin(expire: int, before_refresh: float, at_refresh: float) -> None:
    now = [0.0]
    auth_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"tenant-token-{auth_attempts}", "expire": expire},
            )
        assert request.headers["authorization"] == f"Bearer tenant-token-{auth_attempts}"
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, monotonic=lambda: now[0])

    await client.get_node("page-token")
    now[0] = before_refresh
    await client.get_node("page-token")
    assert auth_attempts == 1

    now[0] = at_refresh
    await client.get_node("page-token")
    assert auth_attempts == 2
    await http_client.aclose()


@pytest.mark.asyncio
async def test_concurrent_first_requests_exchange_tenant_token_once() -> None:
    auth_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    await asyncio.gather(*(client.get_node("page-token") for _ in range(10)))

    assert auth_attempts == 1
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_response",
    [
        httpx.Response(200, content=b"not-json-auth-secret"),
        httpx.Response(200, json={"code": 10003, "msg": "auth-secret", "tenant_access_token": "leaked-token"}),
        httpx.Response(200, json={"code": 0, "expire": 7200}),
        httpx.Response(200, json={"code": 0, "tenant_access_token": "", "expire": 7200}),
        httpx.Response(200, json={"code": 0, "tenant_access_token": 123, "expire": 7200}),
        httpx.Response(200, json={"code": 0, "tenant_access_token": "leaked-token", "expire": True}),
        httpx.Response(200, json={"code": 0, "tenant_access_token": "leaked-token", "expire": 0}),
        httpx.Response(200, json={"code": 0, "tenant_access_token": "leaked-token", "expire": -1}),
        httpx.Response(200, json={"code": 0, "tenant_access_token": "leaked-token", "expire": "7200"}),
        pytest.param(
            httpx.Response(200, json={"code": 0, "tenant_access_token": "leaked-token", "expire": 10**309}),
            id="overflowing-expire",
        ),
    ],
)
async def test_invalid_auth_response_is_normalized_without_secrets(auth_response: httpx.Response) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == TENANT_TOKEN_PATH
        return auth_response

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    with pytest.raises(FeishuAuthenticationError) as raised:
        await client.get_node("page-token")

    exception_text = str(raised.value)
    assert "test-app-secret" not in exception_text
    assert "auth-secret" not in exception_text
    assert "leaked-token" not in exception_text
    assert "not-json-auth-secret" not in exception_text
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    await http_client.aclose()


@pytest.mark.asyncio
async def test_auth_rate_limit_retries_then_succeeds() -> None:
    auth_attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            if auth_attempts == 1:
                return httpx.Response(429, headers={"retry-after": "0.25"})
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, sleep=fake_sleep)

    await client.get_node("page-token")

    assert auth_attempts == 2
    assert delays == [0.25]
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["nan", "inf"])
async def test_auth_non_finite_retry_after_uses_backoff(retry_after: str) -> None:
    auth_attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            if auth_attempts == 1:
                return httpx.Response(429, headers={"retry-after": retry_after})
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, sleep=fake_sleep)

    await client.get_node("page-token")

    assert auth_attempts == 2
    assert delays == [1.0]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_auth_server_error_exhaustion_is_api_error() -> None:
    auth_attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        assert request.url.path == TENANT_TOKEN_PATH
        auth_attempts += 1
        return httpx.Response(503, headers={"x-request-id": "auth-request"}, content=b"auth-secret")

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, max_retries=2, sleep=fake_sleep)

    with pytest.raises(FeishuApiError) as raised:
        await client.get_node("page-token")

    assert auth_attempts == 3
    assert delays == [1.0, 2.0]
    assert raised.value.request_id == "auth-request"
    assert "auth-secret" not in str(raised.value)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_auth_network_error_exhaustion_is_api_error() -> None:
    auth_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        assert request.url.path == TENANT_TOKEN_PATH
        auth_attempts += 1
        raise httpx.ConnectError("network-auth-secret", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, max_retries=2, sleep=lambda delay: asyncio.sleep(0))

    with pytest.raises(FeishuApiError) as raised:
        await client.get_node("page-token")

    assert auth_attempts == 3
    assert "network-auth-secret" not in str(raised.value)
    assert "test-app-secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    await http_client.aclose()


@pytest.mark.asyncio
async def test_first_business_401_refreshes_and_replays_once() -> None:
    auth_attempts = 0
    business_tokens: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"tenant-token-{auth_attempts}", "expire": 7200},
            )
        token = request.headers["authorization"]
        business_tokens.append(token)
        if token == "Bearer tenant-token-1":
            return httpx.Response(401, headers={"x-request-id": "expired-request"})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    node = await client.get_node("page-token")

    assert node.node_token == "page-token"
    assert auth_attempts == 2
    assert business_tokens == ["Bearer tenant-token-1", "Bearer tenant-token-2"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_delayed_old_401_does_not_invalidate_same_value_refreshed_token() -> None:
    auth_attempts = 0
    business_attempts = 0
    second_request_started = asyncio.Event()
    refreshed_token_used = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts, business_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "same-token", "expire": 7200})

        business_attempts += 1
        if business_attempts == 1:
            await second_request_started.wait()
            return httpx.Response(401)
        if business_attempts == 2:
            second_request_started.set()
            await refreshed_token_used.wait()
            return httpx.Response(401)
        if business_attempts == 3:
            refreshed_token_used.set()
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    nodes = await asyncio.gather(client.get_node("page-token"), client.get_node("page-token"))

    assert [node.node_token for node in nodes] == ["page-token", "page-token"]
    assert auth_attempts == 2
    assert business_attempts == 4
    await http_client.aclose()


@pytest.mark.asyncio
async def test_repeated_business_401_stops_after_one_replay() -> None:
    auth_attempts = 0
    business_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts, business_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"tenant-token-{auth_attempts}", "expire": 7200},
            )
        business_attempts += 1
        return httpx.Response(401, headers={"x-request-id": f"business-{business_attempts}"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    with pytest.raises(FeishuAuthenticationError) as raised:
        await client.get_node("page-token")

    assert auth_attempts == 2
    assert business_attempts == 2
    assert raised.value.request_id == "business-2"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_business_403_does_not_refresh_tenant_token() -> None:
    auth_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_attempts
        if request.url.path == TENANT_TOKEN_PATH:
            auth_attempts += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(403, headers={"x-request-id": "forbidden-request"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    with pytest.raises(FeishuPermissionError):
        await client.get_node("page-token")

    assert auth_attempts == 1
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
async def test_business_retryable_status_exhaustion_is_api_error(status_code: int) -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, headers={"x-request-id": "business-request"}, content=b"body-secret")

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = _client(handler, max_retries=2, sleep=fake_sleep)

    with pytest.raises(FeishuApiError) as raised:
        await client.get_node("page-token")

    assert attempts == 3
    assert delays == [1.0, 2.0]
    assert raised.value.status_code == status_code
    assert raised.value.request_id == "business-request"
    assert "body-secret" not in str(raised.value)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["nan", "inf"])
async def test_business_non_finite_retry_after_uses_backoff(retry_after: str) -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": retry_after})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = _client(handler, sleep=fake_sleep)

    await client.get_node("page-token")

    assert attempts == 2
    assert delays == [1.0]
    await client.aclose()


@pytest.mark.asyncio
async def test_business_network_error_exhaustion_is_api_error_without_sensitive_cause() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("business-network-secret", request=request)

    client = _client(handler, max_retries=2, sleep=lambda delay: asyncio.sleep(0))

    with pytest.raises(FeishuApiError) as raised:
        await client.get_node("page-token")

    assert attempts == 3
    assert "business-network-secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    await client.aclose()


@pytest.mark.asyncio
async def test_business_invalid_json_does_not_retain_response_body() -> None:
    client = _client(lambda request: httpx.Response(200, content=b"business-json-secret"))

    with pytest.raises(FeishuApiError) as raised:
        await client.get_node("page-token")

    assert "business-json-secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    await client.aclose()


@pytest.mark.asyncio
async def test_injected_client_does_not_require_a_base_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "open.feishu.cn"
        if request.url.path == TENANT_TOKEN_PATH:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "page-token"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    node = await client.get_node("page-token")

    assert node.node_token == "page-token"
    await client.aclose()


@pytest.mark.asyncio
async def test_aclose_does_not_close_an_injected_client() -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    client = FeishuClient(client=http_client, environ=APP_ENV)

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
async def test_get_wiki_document_reads_docx_content_and_nested_block_attachments() -> None:
    requests: list[tuple[str, str | None, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        requests.append(
            (
                request.url.path,
                request.url.params.get("page_token"),
                request.url.params.get("document_revision_id"),
            )
        )
        if request.url.path == "/open-apis/docx/v1/documents/doc-token":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"document": {"document_id": "doc-token", "revision_id": 42}}},
            )
        if request.url.path.endswith("/raw_content"):
            assert request.url.path == "/open-apis/docx/v1/documents/doc-token/raw_content"
            return httpx.Response(200, json={"code": 0, "data": {"content": "# Hello"}})
        if request.url.path == "/open-apis/docx/v1/documents/doc-token/blocks":
            if request.url.params.get("page_token") is None:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "block_id": "root-1",
                                    "children": ["container-1"],
                                    "block_type": 1,
                                }
                            ],
                            "has_more": True,
                            "page_token": "blocks-next",
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"block_id": "file-block", "file": {"token": "file-1", "name": "guide.pdf"}},
                            {"block_id": "image-block", "image": {"token": "image-1"}},
                        ],
                        "has_more": False,
                    },
                },
            )
        if request.url.path == "/open-apis/docx/v1/documents/doc-token/blocks/root-1/children":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"block_id": "container-1", "children": ["nested-1"]}],
                        "has_more": False,
                    },
                },
            )
        if request.url.path == "/open-apis/docx/v1/documents/doc-token/blocks/container-1/children":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "block_id": "nested-1",
                                "text": {
                                    "elements": [{"file": {"file_token": "inline-1", "source_block_id": "nested-1"}}]
                                },
                            }
                        ],
                        "has_more": False,
                    },
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    client = _client(handler)
    document = await client.get_wiki_document(
        FeishuNode(space_id="space-1", node_token="node-token", obj_token="doc-token", obj_type="docx")
    )

    assert document.content == b"# Hello"
    assert document.revision == "42"
    assert [(item.file_token, item.name, item.file_type) for item in document.attachments] == [
        ("file-1", "guide.pdf", "file"),
        ("image-1", "image-image-1", "image"),
        ("inline-1", "image-inline-1", "image"),
    ]
    assert {item.download_type for item in document.attachments} == {"media"}
    assert requests == [
        ("/open-apis/docx/v1/documents/doc-token", None, None),
        ("/open-apis/docx/v1/documents/doc-token/raw_content", None, None),
        ("/open-apis/docx/v1/documents/doc-token/blocks", None, "42"),
        ("/open-apis/docx/v1/documents/doc-token/blocks", "blocks-next", "42"),
        ("/open-apis/docx/v1/documents/doc-token/blocks/root-1/children", None, "42"),
        ("/open-apis/docx/v1/documents/doc-token/blocks/container-1/children", None, "42"),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_get_wiki_document_rejects_non_docx_nodes() -> None:
    client = _client(lambda request: httpx.Response(200))

    with pytest.raises(FeishuApiError, match="obj_type"):
        await client.get_wiki_document(
            FeishuNode(space_id="space-1", node_token="node-token", obj_token="sheet-token", obj_type="sheet")
        )

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
async def test_download_media_uses_document_media_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/open-apis/drive/v1/medias/media-1/download"
        return httpx.Response(200, content=b"image")

    client = _client(handler)
    download = await client.download("media-1", download_type="media")

    assert download.content == b"image"
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
async def test_not_found_is_normalized() -> None:
    client = _client(lambda request: httpx.Response(404, headers={"x-tt-logid": "request-404"}))

    with pytest.raises(FeishuNotFoundError) as raised:
        await client.get_node("missing-node")

    assert raised.value.status_code == 404
    assert raised.value.request_id == "request-404"
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
    combined_logs = " ".join(log_messages)
    assert APP_ENV["FEISHU_APP_SECRET"] not in combined_logs
    assert "test-tenant-token" not in combined_logs
    assert "Bearer test-tenant-token" not in combined_logs
    assert "response-secret" not in combined_logs
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
