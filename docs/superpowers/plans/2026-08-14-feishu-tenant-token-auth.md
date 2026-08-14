# 飞书 Tenant Token 企业应用认证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用全局企业自建应用的 App ID 与 App Secret 自动获取、缓存和刷新 `tenant_access_token`，替代手工配置访问令牌。

**Architecture:** `FeishuClient` 在实例内懒加载 tenant token，使用单调时钟和异步锁管理刷新，并在业务请求首次 401 后重新认证一次。数据源 API 不再公开凭据变量名，现有数据库列仅保留为非破坏性兼容列；API 与 worker 通过现有 `.env`/Compose `env_file` 获得同一套应用凭据。

**Tech Stack:** Python 3.13、httpx、asyncio、FastAPI、Pydantic、SQLAlchemy、pytest、Vue 3/Vitest。

---

## 文件结构与职责

### 后端认证

- Modify: `backend/package/yuxi/integrations/feishu/client.py`：应用凭据校验、token 换取、缓存、刷新、401 重认证和安全日志。
- Modify: `backend/package/yuxi/integrations/feishu/__init__.py`：导出新的环境变量名常量，移除旧 token 常量。
- Modify: `backend/test/unit/integrations/test_feishu_client.py`：认证、缓存、并发、刷新、错误与脱敏测试。

### 数据源契约

- Modify: `backend/server/routers/feishu_knowledge_router.py`：数据源不再接受或返回凭据变量名，连接检查和 worker 使用全局应用认证。
- Modify: `backend/test/unit/routers/test_feishu_knowledge_router.py`：验证兼容标记、响应脱敏和客户端构造方式。
- Modify: `backend/test/integration/api/test_feishu_knowledge_api_integration.py`：验证管理员 API 的新请求与响应契约。

### 端到端与配置

- Modify: `backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py`：Fake Feishu 增加认证端点，完整链路使用自动换取的 tenant token。
- Modify: `.env.example`：示例变量改为 App ID 与 App Secret。
- Modify: `.env.template`：初始化模板增加飞书企业应用凭据。
- Modify: `docs/advanced/configuration.md`：说明全局应用认证和只读边界。
- Modify: `docs/advanced/deployment.md`：说明 API/worker 配置、重启和认证验收。
- Modify: `docs/implementation/acceptance-log.md`：把阶段二阻塞项改为缺少 App ID/Secret 和真实企业应用验收。

不修改 `docker-compose.yml` 或 `docker-compose.prod.yml`：两个 Compose 文件已通过 `env_file` 将本地环境文件完整注入 API 与 worker。

---

### Task 1: 实现 App ID/Secret 换取与管理 tenant token

**Files:**
- Modify: `backend/package/yuxi/integrations/feishu/client.py`
- Modify: `backend/package/yuxi/integrations/feishu/__init__.py`
- Test: `backend/test/unit/integrations/test_feishu_client.py`

- [ ] **Step 1: 写凭据校验与首次认证失败测试**

在测试文件引入 `asyncio`、`inspect` 和 `json`，定义假凭据，并新增以下测试：

```python
APP_ENV = {
    "FEISHU_APP_ID": "cli_test_app",
    "FEISHU_APP_SECRET": "test-app-secret",
}
AUTH_PATH = "/open-apis/auth/v3/tenant_access_token/internal"


@pytest.mark.parametrize(
    ("environ", "missing_name"),
    [
        ({}, "FEISHU_APP_ID"),
        ({"FEISHU_APP_ID": "cli_test_app"}, "FEISHU_APP_SECRET"),
    ],
)
def test_missing_app_credential_is_explicit(environ: dict[str, str], missing_name: str) -> None:
    with pytest.raises(FeishuCredentialError, match=missing_name):
        FeishuClient(environ=environ)


@pytest.mark.asyncio
async def test_first_request_exchanges_tenant_token_before_business_get() -> None:
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == AUTH_PATH:
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "app_id": "cli_test_app",
                "app_secret": "test-app-secret",
            }
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "tenant_access_token": "tenant-token-1", "expire": 7200},
            )
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer tenant-token-1"
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "root"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)

    node = await client.get_node("root")

    assert node.node_token == "root"
    assert calls == [
        ("POST", AUTH_PATH),
        ("GET", "/open-apis/wiki/v2/spaces/get_node"),
    ]
    await client.aclose()
    await http_client.aclose()
```

- [ ] **Step 2: 运行测试确认正确失败**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q \
  backend/test/unit/integrations/test_feishu_client.py::test_missing_app_credential_is_explicit \
  backend/test/unit/integrations/test_feishu_client.py::test_first_request_exchanges_tenant_token_before_business_get
```

Expected: FAIL，因为客户端仍要求 `FEISHU_ACCESS_TOKEN`，且不会调用认证 POST。

- [ ] **Step 3: 实现最小凭据校验与认证交换**

在 `client.py` 中：

```python
import time

FEISHU_OPEN_API_BASE_URL = "https://open.feishu.cn"
FEISHU_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
DEFAULT_APP_ID_ENV_NAME = "FEISHU_APP_ID"
DEFAULT_APP_SECRET_ENV_NAME = "FEISHU_APP_SECRET"
```

将构造函数改为：

```python
def __init__(
    self,
    *,
    app_id_env_name: str = DEFAULT_APP_ID_ENV_NAME,
    app_secret_env_name: str = DEFAULT_APP_SECRET_ENV_NAME,
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    max_retries: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    environment = os.environ if environ is None else environ
    self._app_id = (environment.get(app_id_env_name) or "").strip()
    self._app_secret = (environment.get(app_secret_env_name) or "").strip()
    missing = [
        name
        for name, value in (
            (app_id_env_name, self._app_id),
            (app_secret_env_name, self._app_secret),
        )
        if not value
    ]
    if missing:
        raise FeishuCredentialError(
            f"Missing Feishu application credential environment variable: {', '.join(missing)}"
        )
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")

    self._owns_client = client is None
    self._client = client or httpx.AsyncClient(base_url=FEISHU_OPEN_API_BASE_URL, timeout=30.0)
    self._max_retries = max_retries
    self._sleep = sleep
    self._monotonic = monotonic
    self._tenant_token: str | None = None
    self._token_refresh_at = 0.0
    self._token_lock = asyncio.Lock()
```

增加最小认证请求和响应解析：

```python
async def _auth_response(self) -> httpx.Response:
    try:
        response = await self._client.post(
            f"{FEISHU_OPEN_API_BASE_URL}{FEISHU_TENANT_TOKEN_PATH}",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
    except httpx.HTTPError as exc:
        raise FeishuApiError("Feishu authentication request failed") from exc
    if response.is_error:
        raise FeishuAuthenticationError(
            "Feishu application authentication failed",
            error=self._error_from_response(response),
        )
    self._log_response(response)
    return response


async def _exchange_tenant_token(self) -> tuple[str, float]:
    response = await self._auth_response()
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuAuthenticationError(
            "Feishu application authentication returned invalid JSON",
            error=self._error_from_response(response),
        ) from exc
    if not isinstance(payload, dict):
        raise FeishuAuthenticationError(
            "Feishu application authentication returned invalid JSON",
            error=self._error_from_response(response),
        )
    code = payload.get("code")
    token = payload.get("tenant_access_token")
    expire = payload.get("expire")
    if code != 0 or not isinstance(token, str) or not token:
        raise FeishuAuthenticationError(
            "Feishu application authentication failed",
            error=FeishuError(
                status_code=response.status_code,
                request_id=self._request_id(response),
                code=code if isinstance(code, int) else None,
            ),
        )
    if isinstance(expire, bool) or not isinstance(expire, (int, float)) or expire <= 0:
        raise FeishuAuthenticationError(
            "Feishu application authentication returned an invalid expiry",
            error=self._error_from_response(response),
        )
    return token, float(expire)


async def _get_tenant_token(self) -> str:
    token, _expire = await self._exchange_tenant_token()
    return token
```

在现有 `_get_response()` 开头使用动态取得的 token：

```python
token = await self._get_tenant_token()
headers = {"Authorization": f"Bearer {token}"}
```

此步骤保留现有业务 GET 重试和错误映射，不提前实现缓存、并发合并或 401 重认证。

- [ ] **Step 4: 运行新增测试确认通过**

Run: 与 Step 2 相同。

Expected: 2 PASS。

- [ ] **Step 5: 写缓存、刷新和并发失败测试**

新增可控单调时钟：

```python
class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds
```

新增三个测试：

```python
@pytest.mark.asyncio
async def test_valid_tenant_token_is_reused_until_refresh_window() -> None:
    auth_calls = 0
    clock = FakeMonotonic()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == AUTH_PATH:
            auth_calls += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"tenant-token-{auth_calls}",
                    "expire": 7200,
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "root"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, monotonic=clock)
    await client.get_node("root")
    await client.get_node("root")
    assert auth_calls == 1

    clock.advance(6901)
    await client.get_node("root")
    assert auth_calls == 2
    await http_client.aclose()


@pytest.mark.asyncio
async def test_short_lived_token_refreshes_at_half_life() -> None:
    auth_calls = 0
    clock = FakeMonotonic()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == AUTH_PATH:
            auth_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"short-{auth_calls}", "expire": 100},
            )
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "root"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, monotonic=clock)
    await client.get_node("root")
    clock.advance(49)
    await client.get_node("root")
    assert auth_calls == 1
    clock.advance(2)
    await client.get_node("root")
    assert auth_calls == 2
    await http_client.aclose()


@pytest.mark.asyncio
async def test_concurrent_first_requests_exchange_token_once() -> None:
    auth_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == AUTH_PATH:
            auth_calls += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "shared-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "root"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)
    await asyncio.gather(client.get_node("root"), client.get_node("root"))
    assert auth_calls == 1
    await http_client.aclose()
```

- [ ] **Step 6: 运行缓存测试确认失败**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q \
  backend/test/unit/integrations/test_feishu_client.py::test_valid_tenant_token_is_reused_until_refresh_window \
  backend/test/unit/integrations/test_feishu_client.py::test_short_lived_token_refreshes_at_half_life \
  backend/test/unit/integrations/test_feishu_client.py::test_concurrent_first_requests_exchange_token_once
```

Expected: FAIL，认证调用次数大于 1。

- [ ] **Step 7: 实现内存缓存、提前刷新和并发合并**

```python
def _token_is_fresh(self) -> bool:
    return bool(self._tenant_token) and self._monotonic() < self._token_refresh_at


async def _get_tenant_token(self) -> str:
    if self._token_is_fresh():
        return self._tenant_token or ""
    async with self._token_lock:
        if self._token_is_fresh():
            return self._tenant_token or ""
        token, expire = await self._exchange_tenant_token()
        refresh_margin = 300.0 if expire >= 600.0 else expire / 2.0
        self._tenant_token = token
        self._token_refresh_at = self._monotonic() + expire - refresh_margin
        return token


def _invalidate_tenant_token(self, expected_token: str) -> None:
    if self._tenant_token == expected_token:
        self._tenant_token = None
        self._token_refresh_at = 0.0
```

- [ ] **Step 8: 运行缓存测试确认通过**

Run: 与 Step 6 相同。

Expected: 3 PASS。

- [ ] **Step 9: 写 401、403、认证重试和脱敏失败测试**

增加以下行为测试，分别使用独立 MockTransport：

```python
@pytest.mark.asyncio
async def test_business_401_refreshes_token_and_replays_once() -> None:
    auth_calls = 0
    business_tokens: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == AUTH_PATH:
            auth_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"tenant-{auth_calls}", "expire": 7200},
            )
        business_tokens.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer tenant-1":
            return httpx.Response(401, headers={"x-request-id": "expired-token"})
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "root"}}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)
    node = await client.get_node("root")
    assert node.node_token == "root"
    assert auth_calls == 2
    assert business_tokens == ["Bearer tenant-1", "Bearer tenant-2"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_repeated_business_401_stops_after_one_reauthentication() -> None:
    auth_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == AUTH_PATH:
            auth_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"tenant-{auth_calls}", "expire": 7200},
            )
        return httpx.Response(401, headers={"x-request-id": "still-unauthorized"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)
    with pytest.raises(FeishuAuthenticationError):
        await client.get_node("root")
    assert auth_calls == 2
    await http_client.aclose()


@pytest.mark.asyncio
async def test_business_403_does_not_refresh_token() -> None:
    auth_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == AUTH_PATH:
            auth_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-1", "expire": 7200},
            )
        return httpx.Response(403, headers={"x-request-id": "permission-denied"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)
    with pytest.raises(FeishuPermissionError):
        await client.get_node("root")
    assert auth_calls == 1
    await http_client.aclose()
```

继续增加以下认证响应校验、重试和脱敏测试：

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_payload", "expected_message"),
    [
        (
            {
                "code": 10003,
                "msg": "auth-response-secret",
                "tenant_access_token": "tenant-token-secret",
                "expire": 7200,
            },
            "authentication failed",
        ),
        ("not-json-auth-response-secret", "invalid JSON"),
        ({"code": 0, "expire": 7200, "detail": "auth-response-secret"}, "authentication failed"),
        (
            {"code": 0, "tenant_access_token": "tenant-token-secret", "expire": True},
            "invalid expiry",
        ),
        (
            {"code": 0, "tenant_access_token": "tenant-token-secret", "expire": 0},
            "invalid expiry",
        ),
        (
            {"code": 0, "tenant_access_token": "tenant-token-secret", "expire": -1},
            "invalid expiry",
        ),
    ],
)
async def test_authentication_response_validation_rejects_invalid_payloads(
    auth_payload: dict[str, object] | str,
    expected_message: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == AUTH_PATH
        if isinstance(auth_payload, dict):
            return httpx.Response(200, json=auth_payload)
        return httpx.Response(
            200,
            content=auth_payload.encode(),
            headers={"content-type": "application/json"},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)
    with pytest.raises(FeishuAuthenticationError, match=expected_message) as raised:
        await client.get_node("root")
    error_text = str(raised.value)
    assert "test-app-secret" not in error_text
    assert "tenant-token-secret" not in error_text
    assert "auth-response-secret" not in error_text
    await http_client.aclose()


@pytest.mark.asyncio
async def test_authentication_rate_limit_retries_then_succeeds() -> None:
    auth_calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == AUTH_PATH:
            auth_calls += 1
            if auth_calls == 1:
                return httpx.Response(429, headers={"retry-after": "0.25"})
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-after-retry", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"node": {"node_token": "root"}}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, sleep=fake_sleep)
    node = await client.get_node("root")
    assert node.node_token == "root"
    assert auth_calls == 2
    assert delays == [0.25]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_authentication_server_errors_exhaust_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_calls = 0
    delays: list[float] = []
    log_messages: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        assert request.url.path == AUTH_PATH
        auth_calls += 1
        return httpx.Response(
            503,
            headers={"x-request-id": "auth-retry"},
            json={"detail": "auth-response-secret", "tenant_access_token": "tenant-token-secret"},
        )

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    def capture_log(message: str, *args: object) -> None:
        log_messages.append(message.format(*args))

    monkeypatch.setattr(feishu_client_module.logger, "info", capture_log)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, max_retries=2, sleep=fake_sleep)
    with pytest.raises(FeishuApiError) as raised:
        await client.get_node("root")
    combined_text = " ".join([*log_messages, str(raised.value)])
    assert auth_calls == 3
    assert delays == [1.0, 2.0]
    assert "test-app-secret" not in combined_text
    assert "tenant-token-secret" not in combined_text
    assert "auth-response-secret" not in combined_text
    await http_client.aclose()


@pytest.mark.asyncio
async def test_authentication_network_errors_exhaust_bounded_retries() -> None:
    auth_calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        assert request.url.path == AUTH_PATH
        auth_calls += 1
        raise httpx.ConnectError("authentication endpoint unavailable", request=request)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV, max_retries=1, sleep=fake_sleep)
    with pytest.raises(FeishuApiError, match="authentication request failed"):
        await client.get_node("root")
    assert auth_calls == 2
    assert delays == [1.0]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_authentication_secrets_never_enter_logs_or_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_messages: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == AUTH_PATH:
            return httpx.Response(
                200,
                headers={"x-request-id": "auth-success"},
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-token-secret",
                    "expire": 7200,
                    "detail": "auth-response-secret",
                },
            )
        return httpx.Response(
            401,
            headers={"x-request-id": "business-unauthorized"},
            json={"detail": "business-response-secret"},
        )

    def capture_log(message: str, *args: object) -> None:
        log_messages.append(message.format(*args))

    monkeypatch.setattr(feishu_client_module.logger, "info", capture_log)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(client=http_client, environ=APP_ENV)
    with pytest.raises(FeishuAuthenticationError) as raised:
        await client.get_node("root")
    combined_text = " ".join([*log_messages, str(raised.value)])
    assert "test-app-secret" not in combined_text
    assert "tenant-token-secret" not in combined_text
    assert "auth-response-secret" not in combined_text
    assert "business-response-secret" not in combined_text
    await http_client.aclose()
```

- [ ] **Step 10: 运行新增安全测试确认失败**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_client.py -k \
  "401 or 403 or authentication or tenant_token or app_credential"
```

Expected: 至少 401 刷新、认证重试或脱敏测试 FAIL。

- [ ] **Step 11: 实现认证请求重试和一次性 401 重放**

把 Step 3 的最小 `_auth_response()` 扩展为有上限重试：

```python
async def _auth_response(self) -> httpx.Response:
    for attempt in range(self._max_retries + 1):
        try:
            response = await self._client.post(
                f"{FEISHU_OPEN_API_BASE_URL}{FEISHU_TENANT_TOKEN_PATH}",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
        except httpx.HTTPError as exc:
            if attempt >= self._max_retries:
                raise FeishuApiError("Feishu authentication request failed") from exc
            await self._sleep(self._backoff_delay(attempt))
            continue

        retryable = response.status_code == 429 or 500 <= response.status_code < 600
        self._log_response(response)
        if retryable:
            if attempt >= self._max_retries:
                raise FeishuApiError(
                    "Feishu authentication request failed",
                    error=self._error_from_response(response),
                )
            await self._sleep(self._retry_delay(response, attempt))
            continue
        if response.is_error:
            raise FeishuAuthenticationError(
                "Feishu application authentication failed",
                error=self._error_from_response(response),
            )
        return response
    raise FeishuApiError("Feishu authentication request exhausted retries")
```

日志只能调用 `_log_response(response)`，不得拼接请求体或响应正文。把现有业务 GET 重试循环抽成：

```python
async def _get_with_retries(
    self,
    path: str,
    *,
    params: dict[str, str] | None,
    token: str,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(self._max_retries + 1):
        try:
            response = await self._client.get(
                f"{FEISHU_OPEN_API_BASE_URL}{path}",
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            if attempt >= self._max_retries:
                raise FeishuApiError("Feishu request failed") from exc
            await self._sleep(self._backoff_delay(attempt))
            continue
        retryable = response.status_code == 429 or 500 <= response.status_code < 600
        if retryable and attempt < self._max_retries:
            self._log_response(response)
            await self._sleep(self._retry_delay(response, attempt))
            continue
        return response
    raise FeishuApiError("Feishu request exhausted retries")
```

将 `_get_response()` 的业务请求包在最多两轮认证循环中：

```python
async def _get_response(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
    for auth_attempt in range(2):
        token = await self._get_tenant_token()
        response = await self._get_with_retries(path, params=params, token=token)
        if response.status_code == 401 and auth_attempt == 0:
            self._invalidate_tenant_token(token)
            continue
        if response.status_code == 401:
            raise FeishuAuthenticationError(
                "Feishu authentication failed",
                error=self._error_from_response(response),
            )
        if response.status_code == 403:
            raise FeishuPermissionError(
                "Feishu permission denied",
                error=self._error_from_response(response),
            )
        if response.status_code == 404:
            raise FeishuNotFoundError(
                "Feishu resource not found",
                error=self._error_from_response(response),
            )
        if response.is_error:
            raise FeishuApiError("Feishu request failed", error=self._error_from_response(response))
        self._log_response(response)
        return response
    raise FeishuAuthenticationError("Feishu authentication failed")
```

`_get_with_retries()` 负责 GET 网络错误、429 和 5xx，但把最终 HTTP response 返回给 `_get_response()` 统一映射。

- [ ] **Step 12: 更新现有客户端测试辅助器和导出**

把现有 `_client(handler)` 改为先处理认证端点，再转发给原 handler；同步和异步 handler 都要支持：

```python
def _client(handler, **kwargs) -> FeishuClient:
    async def authenticated_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == AUTH_PATH:
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "test-tenant-token", "expire": 7200},
            )
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    transport = httpx.MockTransport(authenticated_handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://open.feishu.test")
    return FeishuClient(client=http_client, environ=APP_ENV, **kwargs)
```

直接构造 `FeishuClient` 的现有测试也改用 `APP_ENV` 并处理认证 POST。更新 `yuxi.integrations.feishu.__init__`：

```python
from yuxi.integrations.feishu.client import (
    DEFAULT_APP_ID_ENV_NAME,
    DEFAULT_APP_SECRET_ENV_NAME,
    FeishuApiError,
    FeishuAuthenticationError,
    FeishuClient,
    FeishuClientError,
    FeishuCredentialError,
    FeishuNotFoundError,
    FeishuPermissionError,
)
```

`__all__` 同步导出两个新常量并删除 `DEFAULT_CREDENTIAL_ENV_NAME`。

- [ ] **Step 13: 运行完整客户端测试与 Ruff**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_client.py
uv run --project backend --no-sync --no-dev ruff check \
  backend/package/yuxi/integrations/feishu/client.py \
  backend/package/yuxi/integrations/feishu/__init__.py \
  backend/test/unit/integrations/test_feishu_client.py
uv run --project backend --no-sync --no-dev ruff format --check \
  backend/package/yuxi/integrations/feishu/client.py \
  backend/package/yuxi/integrations/feishu/__init__.py \
  backend/test/unit/integrations/test_feishu_client.py
```

Expected: 全部 exit 0。

- [ ] **Step 14: 提交客户端认证**

```bash
git add \
  backend/package/yuxi/integrations/feishu/client.py \
  backend/package/yuxi/integrations/feishu/__init__.py \
  backend/test/unit/integrations/test_feishu_client.py
git commit -m "feat: authenticate Feishu with tenant token"
```

---

### Task 2: 将数据源 API 切换为全局应用认证

**Files:**
- Modify: `backend/server/routers/feishu_knowledge_router.py`
- Test: `backend/test/unit/routers/test_feishu_knowledge_router.py`
- Test: `backend/test/integration/api/test_feishu_knowledge_api_integration.py`

- [ ] **Step 1: 修改单元测试表达新契约**

把 `test_create_source_persists_only_credential_environment_name` 改为：

```python
async def test_create_source_uses_global_app_marker_and_hides_legacy_credential(monkeypatch):
    captured = {}

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_or_create_source(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(**kwargs)

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    payload = router_module.SourceCreate.model_validate(
        {
            "name": "Docs",
            "wiki_root_token": "root",
            "target_kb_id": "kb-1",
            "credential_env_name": "LEGACY_TOKEN_NAME",
        }
    )
    result = await router_module.create_source(
        payload,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(uid="admin"),
    )

    assert captured["credential_env_name"] == "GLOBAL_FEISHU_APP"
    assert "credential_env_name" not in result
    assert captured["created_by"] == "admin"
```

把连接检查测试改成下面的无参数客户端，并保留 source 中的旧值，证明该字段不会被读取或传递：

```python
class FakeClient:
    def __init__(self):
        calls.append(("init",))

    async def get_node(self, node_token):
        calls.append(("get", node_token))
        return SimpleNamespace(node_token=node_token, title="Root")

    async def aclose(self):
        calls.append(("close",))


assert calls == [("init",), ("get", "root"), ("close",)]
```

同步修改本文件中所有替换 `router_module.FeishuClient` 的 `FakeClient` / `FailingClient`，包括扫描 worker、取消扫描、初始化失败和连接检查测试：

```python
class FakeClient:
    def __init__(self):
        pass

    async def aclose(self):
        pass


class FailingClient:
    def __init__(self):
        raise RuntimeError("credential unavailable")
```

将 `lambda **kwargs: FakeClient()` 和 `lambda **kwargs: _FailingAfterArchiveFeishuClient()` 分别替换为 `FakeClient` 和 `_FailingAfterArchiveFeishuClient`。原先连接检查中两个接收 `credential_env_name` 的 `FailingClient` 也都改成 `def __init__(self)`；初始化错误测试固定抛出 `FeishuClientError("Missing Feishu application credentials")`，根节点不可读测试继续在 `get_node()` 中抛错。完成后以下检查必须无输出：

```bash
rg -n -U \
  'class (FakeClient|FailingClient):\n\s+def __init__\(self, \*\*kwargs\)|credential_env_name\)|lambda \*\*kwargs: .*Client' \
  backend/test/unit/routers/test_feishu_knowledge_router.py
```

- [ ] **Step 2: 修改 API 集成测试表达新响应**

在 `test_admin_create_check_scan_query_reject_and_approve_contracts` 中使用无参数客户端：

```python
class FakeFeishuClient:
    def __init__(self):
        calls.append(("client",))

    async def get_node(self, token):
        calls.append(("get_node", token))
        return SimpleNamespace(title="Root")

    async def aclose(self):
        calls.append(("close",))
```

并把响应及 repository 断言改为：

```python
assert "credential_env_name" not in created.json()
assert "credential_env_name" not in queried.json()["items"][0]
create_call = next(call for call in calls if call[0] == "create")
assert create_call[1]["credential_env_name"] == "GLOBAL_FEISHU_APP"
assert ("client",) in calls
```

把同一文件末尾真实网络测试的旧 token 门禁改为企业应用凭据门禁：

```python
@pytest.mark.skipif(
    not (os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET")),
    reason="Real Feishu application credentials are not configured",
)
async def test_real_feishu_network_scan_is_deferred_to_task_6_e2e():
    pytest.skip("Task 6 owns the opt-in real Feishu network scan")
```

- [ ] **Step 3: 运行路由测试确认失败**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py \
  -k "create_source or check_source or admin_create_check_scan"
```

Expected: FAIL，因为当前路由仍公开并传递 `credential_env_name`。

- [ ] **Step 4: 实现全局认证数据源契约**

在 router 模块常量区增加：

```python
GLOBAL_FEISHU_CREDENTIAL_MARKER = "GLOBAL_FEISHU_APP"
```

`SourceCreate` 删除 `credential_env_name` 字段，并把 validator 改为：

```python
@field_validator("name", "wiki_root_token", "target_kb_id")
@classmethod
def identifiers_must_not_be_blank(cls, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("value must not be blank")
    return value
```

`create_source()` 向 repository 传入固定标记：

```python
credential_env_name=GLOBAL_FEISHU_CREDENTIAL_MARKER,
```

`_source_dict()` 删除 `credential_env_name`。连接检查和扫描 worker 均改为：

```python
client = FeishuClient()
```

数据库模型、schema 和 repository 签名保持不变。

- [ ] **Step 5: 运行路由专项和相关初始化失败测试**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py
```

Expected: 全部 PASS。初始化缺少全局应用凭据时，连接检查仍映射为 HTTP 422；worker 初始化失败仍将批次标记为 failed。

- [ ] **Step 6: 运行 Ruff 并提交**

```bash
uv run --project backend --no-sync --no-dev ruff check \
  backend/server/routers/feishu_knowledge_router.py \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py
uv run --project backend --no-sync --no-dev ruff format --check \
  backend/server/routers/feishu_knowledge_router.py \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py
git add \
  backend/server/routers/feishu_knowledge_router.py \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py
git commit -m "refactor: use global Feishu app credentials"
```

Expected: 检查 exit 0，提交只包含三个指定文件。

---

### Task 3: 让离线 E2E 经过真实认证代码路径

**Files:**
- Modify: `backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py`

- [ ] **Step 1: 修改 Fake Feishu transport 并先写认证断言**

在标准库导入区增加 `import json`，然后替换测试常量：

```python
OFFLINE_APP_ID = "cli_offline_app"
OFFLINE_APP_SECRET = "offline-app-secret"
OFFLINE_TOKEN = "offline-tenant-token"
AUTH_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
```

`FakeFeishuTransport.__init__` 增加 `self.auth_calls = 0`。在 transport 最前面处理认证：

```python
if request.url.path == AUTH_PATH:
    assert request.method == "POST"
    assert request.headers.get("authorization") is None
    assert request.read()
    assert request.content
    payload = json.loads(request.content)
    assert payload == {
        "app_id": OFFLINE_APP_ID,
        "app_secret": OFFLINE_APP_SECRET,
    }
    self.auth_calls += 1
    return httpx.Response(
        200,
        json={
            "code": 0,
            "tenant_access_token": OFFLINE_TOKEN,
            "expire": 7200,
        },
    )

assert request.method == "GET"
assert request.headers["authorization"] == f"Bearer {OFFLINE_TOKEN}"
```

`OfflinePipeline.scan()` 改为：

```python
client = FeishuClient(
    client=http_client,
    environ={
        "FEISHU_APP_ID": OFFLINE_APP_ID,
        "FEISHU_APP_SECRET": OFFLINE_APP_SECRET,
    },
    sleep=_no_sleep,
)
```

数据源兼容列使用 `GLOBAL_FEISHU_APP`。完整扫描测试断言一次扫描只发生一次认证。日志脱敏测试同时断言假 App Secret 和 tenant token 均未出现。

- [ ] **Step 2: 运行 E2E 确认旧实现失败**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
```

Expected: 在 Task 1 尚未合入时会因不支持 App ID/Secret 或没有认证 POST 而 FAIL；Task 1 已完成后应直接 PASS。若直接 PASS，确认测试确实断言了认证调用次数、POST 请求体和业务 Authorization，而不是仅替换环境变量。

- [ ] **Step 3: 完成 E2E 认证夹具适配并验证**

只修正测试夹具与新客户端契约的差异，不修改扫描、审核或发布生产逻辑。

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
uv run --project backend --no-sync --no-dev ruff check backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
uv run --project backend --no-sync --no-dev ruff format --check backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
```

Expected: `4 passed`，Ruff exit 0；现有两条依赖弃用警告可以保留记录。

- [ ] **Step 4: 提交 E2E 认证覆盖**

```bash
git add backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
git commit -m "test: cover Feishu tenant token flow end to end"
```

---

### Task 4: 更新本机配置、部署说明和验收阻塞项

**Files:**
- Modify: `.env.example`
- Modify: `.env.template`
- Modify: `docs/advanced/configuration.md`
- Modify: `docs/advanced/deployment.md`
- Modify: `docs/implementation/acceptance-log.md`

- [ ] **Step 1: 更新环境变量示例**

`.env.example` 将旧变量替换为：

```env
FEISHU_APP_ID=
FEISHU_APP_SECRET=
```

`.env.template` 在生产凭据区增加相同两个空变量，并注明只用于飞书企业自建应用，不写入 Git 中的真实值。

- [ ] **Step 2: 更新配置和部署文档**

`configuration.md` 明确：

- API 和 worker 读取固定的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`
- 后端自动换取并仅在进程内缓存 tenant token
- 认证 POST 是唯一非 GET 调用，且不改变企业内容
- 数据源不保存凭据变量名或凭据正文

`deployment.md` 将旧 token 变量替换为 App ID/Secret，说明二者必须同时配置到 API 和 worker 使用的同一个本机环境文件；修改后只重启 API/worker，不重置任何命名卷。

- [ ] **Step 3: 更新阶段二验收记录但保持 RUNNING**

将阻塞项写成：

```markdown
- 待完成：当前未配置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，尚未执行企业自建应用认证、真实飞书连接、限定范围权限验证和完整扫描
```

离线证据增加“已覆盖 tenant token 换取、缓存和脱敏”，但不得写真实认证已通过，不得将阶段二改为 PASS。

- [ ] **Step 4: 验证文档没有残留运行配置**

Run:

```bash
if rg -n "FEISHU_ACCESS_TOKEN" \
  .env.example \
  .env.template \
  docs/advanced/configuration.md \
  docs/advanced/deployment.md \
  docs/implementation/acceptance-log.md; then
  exit 1
fi
git diff --check
```

Expected: `rg` 无输出，命令 exit 0；历史设计和历史计划中的旧变量说明不在本检查范围内。

- [ ] **Step 5: 提交配置与文档**

```bash
git add \
  .env.example \
  .env.template \
  docs/advanced/configuration.md \
  docs/advanced/deployment.md \
  docs/implementation/acceptance-log.md
git commit -m "docs: configure Feishu enterprise app credentials"
```

---

### Task 5: 完整回归与真实企业应用验收

**Files:**
- Verify: 所有 Task 1-4 文件
- Conditionally modify: `docs/implementation/acceptance-log.md`

- [ ] **Step 1: 运行后端完整 Feishu 专项**

```bash
uv run --project backend --no-sync --no-dev pytest -q \
  backend/test/unit/integrations \
  backend/test/unit/repositories/test_feishu_knowledge_repository.py \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py \
  backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
```

Expected: 全部 PASS，无失败或 error。

- [ ] **Step 2: 运行静态检查和前端回归**

```bash
uv run --project backend --no-sync --no-dev ruff check \
  backend/package/yuxi/integrations/feishu \
  backend/server/routers/feishu_knowledge_router.py \
  backend/test/unit/integrations/test_feishu_client.py \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py \
  backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
uv run --project backend --no-sync --no-dev ruff format --check \
  backend/package/yuxi/integrations/feishu \
  backend/server/routers/feishu_knowledge_router.py \
  backend/test/unit/integrations/test_feishu_client.py \
  backend/test/unit/routers/test_feishu_knowledge_router.py \
  backend/test/integration/api/test_feishu_knowledge_api_integration.py \
  backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
pnpm --dir web exec vitest run src/views/FeishuKnowledgeView.spec.js
pnpm --dir web build
git diff --check
```

Expected: 全部 exit 0。构建中既有 `:deep`、依赖注解和大 chunk 警告可记录，但不能有构建失败。

- [ ] **Step 3: 检查提交和用户工作区边界**

```bash
git status --short --branch
git diff --cached --name-only
git log --oneline --decorate -8
```

Expected: 不存在暂存文件；本任务提交只包含计划列出的文件；保留且不提交用户已有的：

```text
backend/package/yuxi/storage/minio/client.py
backend/test/unit/storage/test_minio_public_images.py
```

- [ ] **Step 4: 本机凭据配置门禁**

用户只在项目根目录 `.env` 中写入真实值，不把值粘贴到聊天。使用不输出内容的检查：

```bash
for name in FEISHU_APP_ID FEISHU_APP_SECRET; do
  if ! awk -F= -v key="$name" '
    $1 == key {
      value = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (length(value) > 0) found = 1
    }
    END { exit !found }
  ' .env; then
    echo "$name is not configured"
    exit 1
  fi
done
echo "Feishu application credentials are configured"
```

Expected: 只输出配置状态，不输出 App ID 或 App Secret。若门禁失败，停止真实验收，阶段二保持 RUNNING。

- [ ] **Step 5: 只重启源码 API 与 worker**

先用只读命令解析目标进程，确认不会命中旧 Docker `5050/5173`：

```bash
lsof -nP -iTCP:5051 -sTCP:LISTEN
pgrep -af "arq server.worker_main.WorkerSettings"
```

仅终止上述已核实的源码 API/worker 进程，然后在两个独立托管终端会话中启动：

```bash
uv run --project backend --no-sync --no-dev uvicorn server.main:app --host 127.0.0.1 --port 5051
```

```bash
uv run --project backend --no-sync --no-dev arq server.worker_main.WorkerSettings
```

Expected: `http://127.0.0.1:5051/api/system/health` 返回 healthy，worker 完成启动；旧 Docker 服务保持运行，不执行 `docker compose down` 或任何卷删除命令。

- [ ] **Step 6: 执行真实连接与限定扫描**

在已登录的 `http://localhost:5175/feishu-knowledge` 工作台执行：

1. 检查连接。
2. 核对根节点标题，不复制企业正文到日志或聊天。
3. 先确认应用仅具备目标知识空间和根目录所需权限。
4. 启动一次受控全量扫描。
5. 核对目录层级数量和 PDF/TXT/PNG/音频/视频类型统计。
6. 在确认扫描范围正确前不批准发布。

Expected: 认证成功、根节点可读、批次完成且没有越权目录；日志不含 App Secret 或 tenant token。

- [ ] **Step 7: 验证重启持久化并更新验收记录**

只再次重启源码 API 与 worker，不重置 PostgreSQL、Redis、MinIO、Milvus 或 etcd。核对同步批次、素材版本、审核状态、active 版本、对象和检索引用仍存在。

只有真实连接、扫描和重启持久化全部具备证据时，才把阶段二改为 PASS 并提交：

```bash
git add docs/implementation/acceptance-log.md
git commit -m "docs: record Feishu enterprise app acceptance"
```

任一项未完成时保持 RUNNING，只记录具体阻塞，不创建虚假 PASS 提交。

---

## 计划自检

- 规格覆盖：全局 App ID/Secret、认证 POST、内存缓存、提前刷新、并发锁、401 单次重放、403 权限错误、429/5xx 重试、API/worker、兼容列、安全、E2E 和真实验收均有对应步骤。
- 类型一致性：环境变量固定为 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`；缓存字段统一为 `_tenant_token`、`_token_refresh_at`；兼容标记统一为 `GLOBAL_FEISHU_APP`。
- 非破坏性：不删除 `credential_env_name` 列，不重建数据库，不停止旧 Docker，不删除命名卷。
- 敏感信息：所有自动化测试使用假值；检查命令只输出是否配置，不输出真实凭据；真实企业正文不进入测试、文档或提交。
- 范围控制：不支持手工 token 回退、多企业、多应用或 Redis token 缓存，不修改无关前端布局。
