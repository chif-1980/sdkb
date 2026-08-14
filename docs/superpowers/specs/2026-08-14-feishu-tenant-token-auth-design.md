# 飞书企业自建应用认证设计

## 目标

将飞书知识流水线从“管理员手工提供长期可用的访问令牌”改为企业自建应用认证。API 和 worker 仅从本机环境读取一套 App ID 与 App Secret，自动换取、缓存并刷新 `tenant_access_token`，再使用该 token 访问现有只读飞书接口。

完成后，管理员不需要也不能在系统中录入 `tenant_access_token`。

## 范围

本次只支持一个飞书企业和一套企业自建应用，所有飞书知识数据源共用：

```env
FEISHU_APP_ID=
FEISHU_APP_SECRET=
```

不在本次范围内：

- 多企业或多套飞书应用
- 手工 `FEISHU_ACCESS_TOKEN` 兼容模式
- Redis、PostgreSQL 或文件持久化 token
- 在管理界面录入或展示 App ID、App Secret
- 任何修改飞书企业内容的接口

## 认证架构

### 客户端职责

`FeishuClient` 继续负责现有 Wiki、Docx 和 Drive 只读访问，同时增加企业应用认证能力：

1. 初始化时从环境读取 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。
2. 任一变量缺失时立即抛出 `FeishuCredentialError`，错误只指出缺失的变量名。
3. 第一次业务请求前调用：

   ```text
   POST /open-apis/auth/v3/tenant_access_token/internal
   ```

4. 请求体仅包含 `app_id` 和 `app_secret`。
5. 从成功响应中读取 `tenant_access_token` 和 `expire`。
6. 后续 Wiki、Docx 和 Drive 请求继续使用 `Authorization: Bearer <token>`。

认证 POST 只用于获取访问凭据，不改变飞书业务数据。除认证请求外，现有飞书业务接口仍只允许 GET。

### token 缓存与刷新

token 只保存在 `FeishuClient` 实例内存中，不写入数据库、Redis、文件或日志。

- 使用单调时钟计算有效期，避免系统时间调整影响刷新。
- 正常有效期提前 300 秒刷新。
- 当有效期不足 600 秒时，在有效期过半时刷新，避免短有效期造成立即刷新循环。
- 同一客户端的并发请求通过 `asyncio.Lock` 合并认证，只允许一个请求换取 token。
- 长时间扫描期间，每次发起业务请求前都检查有效期，因此 token 可在同一批次中自动刷新。
- 客户端关闭后丢弃 token；API 和 worker 重启后重新认证。

API 和 worker 是独立进程，各自维护内存 token，不共享缓存。这避免引入分布式锁和额外持久化状态。

### 401 处理

业务请求收到 401 时：

1. 使当前缓存 token 失效。
2. 强制重新认证。
3. 使用新 token 重放原业务请求一次。
4. 第二次仍为 401 时抛出 `FeishuAuthenticationError`，不再循环重试。

403 表示应用或资源权限不足，不刷新 token，直接抛出 `FeishuPermissionError`。

429 和 5xx 继续使用现有有上限重试逻辑。认证接口同样执行有上限重试，但不会把认证响应正文写入日志。

## 数据源兼容

认证配置属于部署环境，不属于某个知识数据源。

- 新建数据源的公开请求模型移除 `credential_env_name`；迁移期旧调用方多传该字段时按现有 Pydantic 行为忽略，不再把它作为有效配置。
- 数据源 API 响应不再返回 `credential_env_name`。
- 连接检查和扫描 worker 直接创建使用全局 App ID/Secret 的 `FeishuClient`。
- PostgreSQL 现有 `credential_env_name` 列暂时保留，不执行删除列或重建表。
- 新数据源向该兼容列写入固定内部标记 `GLOBAL_FEISHU_APP`；已有数据值保留，但认证流程不再读取。

保留旧列是为了避免当前阶段引入破坏性 schema 迁移。该列不再构成公开配置契约。

## 错误与安全

### 错误映射

- 缺少 App ID 或 App Secret：`FeishuCredentialError`
- 认证接口拒绝凭据或返回无效 token：`FeishuAuthenticationError`
- 飞书资源权限不足：`FeishuPermissionError`
- 认证或业务接口网络错误、429、5xx 重试耗尽：`FeishuApiError`

连接检查 API 保持现有行为，将客户端错误转换为 HTTP 422，并返回不含敏感信息的说明。

### 敏感信息边界

以下内容不得进入日志、异常文本、数据库、API 响应、测试快照或 Git：

- App Secret
- `tenant_access_token`
- Authorization 请求头
- 认证接口请求体和响应正文

日志只允许记录请求类型、HTTP 状态、飞书 request id 和不敏感的业务错误码。单元测试使用明确的假凭据，并验证日志中不存在假 App Secret 和假 token。

## 配置与界面

- `.env.example`、部署文档和配置文档改为 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。
- 删除对管理员手工配置 `FEISHU_ACCESS_TOKEN` 的说明。
- 工作台不新增凭据输入框，也不展示凭据是否存在的具体值。
- “检查连接”仍是管理员验证应用凭据与根节点权限的入口。

## 测试设计

### 客户端单元测试

使用 `httpx.MockTransport` 验证：

- 缺少任一环境变量时初始化失败
- 认证 POST 的路径、方法与请求字段正确
- 首次请求先认证，再携带 tenant token 发起业务 GET
- 有效期内多个请求只认证一次
- 临近过期时自动刷新
- 并发首次请求只认证一次
- 业务请求 401 后刷新并只重放一次
- 403 不刷新
- 认证接口业务错误、无效 JSON、缺少 token、无效 `expire`
- 认证接口 429 和 5xx 的有上限重试
- App Secret、tenant token 和响应正文不进入日志或异常

### 路由与流水线回归

- 连接检查和扫描 worker 不再读取数据源的旧凭据变量名。
- 新建数据源不再使用或返回凭据变量配置；迁移期旧请求中的该字段被忽略。
- 现有递归扫描、附件下载、审核、发布和来源引用测试继续通过。
- 离线 E2E 的 Fake Feishu transport 增加认证端点，并验证完整流程使用换取的 tenant token。

## 真实验收

管理员在项目根目录本机 `.env` 中保存 App ID 与 App Secret，且不把值发送到聊天：

```env
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
```

随后只重启源码 API 和 worker，并依次执行：

1. 检查连接，确认认证成功且根节点可读。
2. 验证目标知识空间和根目录的应用权限范围。
3. 执行受控全量扫描。
4. 核对目录层级、附件发现、加工状态和来源引用。
5. 再次只重启 API 和 worker，验证状态与正式知识仍可检索。

只有这些真实证据齐全后，阶段二验收记录才能从 `RUNNING` 改为 `PASS`。
