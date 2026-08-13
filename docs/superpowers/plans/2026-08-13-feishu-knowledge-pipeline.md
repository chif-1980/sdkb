# 飞书知识加工流水线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Yuxi 内实现飞书 Wiki 只读递归扫描、素材版本追踪、人工审核和发布到独立 `Quickdone 企业知识库` 的阶段二流水线。

**Architecture:** 新增 Feishu integration client、PostgreSQL 状态模型/仓储、任务化同步与加工服务，以及管理员工作台 API/UI。飞书数据只读；原始和解析文件进入 MinIO；PostgreSQL 保存唯一状态源；审核通过后复用现有 Yuxi 文档解析与 Milvus 入库链路，完成 active 版本原子切换。

**Tech Stack:** Python 3.13、FastAPI、SQLAlchemy async、PostgreSQL、httpx、Yuxi Tasker、MinIO、Milvus、Vue 3、Ant Design Vue、Vitest、pytest。

---

## 文件结构与职责

### 后端新增

- `backend/package/yuxi/integrations/feishu/client.py`：飞书 API 客户端、分页、限流/重试和错误归一化。
- `backend/package/yuxi/integrations/feishu/schemas.py`：飞书页面、附件、扫描项的内部数据结构。
- `backend/package/yuxi/integrations/feishu/service.py`：根节点递归扫描、版本判定、下载和来源失效计算。
- `backend/package/yuxi/integrations/feishu/__init__.py`：公开 integration API。
- `backend/package/yuxi/repositories/feishu_knowledge_repository.py`：数据源、批次、素材、版本、事件的查询和事务操作。
- `backend/server/routers/feishu_knowledge_router.py`：管理员工作台 API。

### 后端修改

- `backend/package/yuxi/storage/postgres/models_knowledge.py`：新增 5 张 Feishu 状态表模型。
- `backend/package/yuxi/storage/postgres/manager.py`：增加 schema 演进 SQL、索引和约束。
- `backend/server/routers/__init__.py`：注册 `/api/feishu-knowledge/*`。
- `backend/server/utils/lifespan.py`：确认新增表随知识 schema 创建；不启动定时同步。
- `backend/package/yuxi/config/app.py` 或新增配置模块：声明可公开展示的 Feishu 根节点和凭据变量名配置，绝不保存凭据正文。
- `backend/test/...`：为客户端、扫描、状态流转、API 和持久化补齐测试。

### 前端新增/修改

- `web/src/apis/feishu_knowledge_api.js`：数据源、批次、素材和审核操作 API。
- `web/src/views/FeishuKnowledgeView.vue`：数据源总览和全量/增量同步入口。
- `web/src/components/feishu/FeishuSyncRunsTable.vue`：批次列表和批次详情。
- `web/src/components/feishu/FeishuMaterialTable.vue`：状态筛选、素材列表和审核/重试/下架操作。
- `web/src/components/feishu/FeishuMaterialDetailDrawer.vue`：来源、版本、Markdown、Chunk、事件时间线。
- `web/src/router/index.js`：新增管理员路由 `/feishu-knowledge`。
- `web/src/layouts/AppLayout.vue`：管理员导航增加“知识加工”。
- `web/src/**/*.spec.js`：Vitest 组件和 API 状态测试。

### 文档/验收

- `docs/advanced/configuration.md`：记录 Feishu 凭据变量名和只读边界。
- `docs/advanced/deployment.md`：记录阶段二环境变量和正式知识库初始化要求。
- `docs/implementation/acceptance-log.md`：阶段二逐项记录证据，不在完成前提前写 PASS。

---

### Task 1: 建立 Feishu 状态数据模型与 schema 演进

**Files:**
- Modify: `backend/package/yuxi/storage/postgres/models_knowledge.py`
- Modify: `backend/package/yuxi/storage/postgres/manager.py`
- Create: `backend/test/unit/storage/test_feishu_knowledge_models.py`
- Create: `backend/test/unit/storage/test_feishu_schema.py`

- [ ] **Step 1: 写模型约束失败测试**

覆盖以下约束：稳定对象键唯一、版本 `(item_id, revision, content_hash)` 唯一、同一素材只有一个 active 版本、事件可追加、音视频状态可保存。

```python
def test_feishu_material_version_has_stable_identity_and_active_fields():
    columns = FeishuMaterialVersion.__table__.columns
    assert {"item_id", "revision", "content_hash", "review_status", "published_at"} <= set(columns.keys())

def test_feishu_source_item_item_key_is_unique():
    constraints = FeishuSourceItem.__table__.constraints
    assert any("item_key" in {column.name for column in c.columns} for c in constraints if hasattr(c, "columns"))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/storage/test_feishu_knowledge_models.py backend/test/unit/storage/test_feishu_schema.py`

Expected: FAIL because the five Feishu models and schema statements do not exist.

- [ ] **Step 3: 实现五张表**

在 `models_knowledge.py` 中新增 `FeishuSource`、`FeishuSyncRun`、`FeishuSourceItem`、`FeishuMaterialVersion`、`FeishuProcessingEvent`。使用字符串状态字段、JSONB payload、时间字段和必要索引；外键均指向 `source_id/item_id/version_id`，删除策略只允许清理同步元数据，不得级联删除正式 `KnowledgeFile`。

- [ ] **Step 4: 添加启动 schema 演进**

在 `ensure_knowledge_schema()` 中加入 `CREATE TABLE IF NOT EXISTS` 和 `CREATE INDEX IF NOT EXISTS`，保证现有安装升级可重复执行；不要调用 drop 或 reset。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/storage/test_feishu_knowledge_models.py backend/test/unit/storage/test_feishu_schema.py`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/package/yuxi/storage/postgres/models_knowledge.py backend/package/yuxi/storage/postgres/manager.py backend/test/unit/storage/test_feishu_knowledge_models.py backend/test/unit/storage/test_feishu_schema.py
git commit -m "feat: add Feishu knowledge pipeline schema"
```

### Task 2: 实现 Feishu API 客户端和凭据解析

**Files:**
- Create: `backend/package/yuxi/integrations/feishu/client.py`
- Create: `backend/package/yuxi/integrations/feishu/schemas.py`
- Create: `backend/package/yuxi/integrations/feishu/__init__.py`
- Create: `backend/test/unit/integrations/test_feishu_client.py`
- Modify: `docs/advanced/configuration.md`

- [ ] **Step 1: 写客户端失败测试**

使用 `httpx.MockTransport` 覆盖：分页游标、页面详情、子节点、附件列表、下载、401/403、429 `Retry-After`、5xx 重试和缺少凭据。

```python
@pytest.mark.asyncio
async def test_list_children_follows_page_token(feishu_client):
    nodes = await feishu_client.list_children("root-token")
    assert [node.node_token for node in nodes] == ["child-1", "child-2"]

@pytest.mark.asyncio
async def test_missing_credential_is_explicit():
    with pytest.raises(FeishuCredentialError, match="FEISHU_ACCESS_TOKEN"):
        FeishuClient(credential_env_name="FEISHU_ACCESS_TOKEN", environ={})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_client.py`

Expected: FAIL because the client does not exist.

- [ ] **Step 3: 实现只读客户端**

实现 `FeishuClient`：从配置的 `credential_env_name` 读取 token，默认变量名为 `FEISHU_ACCESS_TOKEN`；支持通过依赖注入传入 `httpx.AsyncClient`，生产端点使用 Feishu Open API；所有请求只允许 GET。对 401/403 转换为鉴权/权限错误，对 429 和 5xx 做有上限的指数退避；响应日志只记录状态码和 request id，不记录 Authorization 或响应正文中的 token。

- [ ] **Step 4: 写内部 schema 和配置说明**

定义 `FeishuNode`、`FeishuAttachment`、`FeishuDownload`、`FeishuApiError` 等不可变数据结构；文档说明凭据变量名可配置、默认 `FEISHU_ACCESS_TOKEN`，真实部署前必须由管理员在本机环境中配置，不将值提交到仓库。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_client.py`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/package/yuxi/integrations/feishu backend/test/unit/integrations/test_feishu_client.py docs/advanced/configuration.md
git commit -m "feat: add read-only Feishu client"
```

### Task 3: 实现递归扫描、版本判定和来源失效保护

**Files:**
- Create: `backend/package/yuxi/integrations/feishu/service.py`
- Create: `backend/package/yuxi/repositories/feishu_knowledge_repository.py`
- Create: `backend/test/unit/integrations/test_feishu_scan_service.py`
- Create: `backend/test/unit/repositories/test_feishu_knowledge_repository.py`

- [ ] **Step 1: 写扫描和版本判断失败测试**

覆盖：深层递归、页面附件拆成独立素材、同 revision/哈希跳过、revision 变更创建新版本、音视频标记暂不支持、完整扫描才允许来源失效、权限失败不批量失效。

```python
@pytest.mark.asyncio
async def test_incremental_scan_creates_version_only_for_changed_item(repository, fake_feishu):
    result = await service.scan(source_id="source-1", mode="incremental")
    assert result.changed_count == 1
    assert result.unchanged_count == 1
    assert result.unsupported_count == 1

@pytest.mark.asyncio
async def test_partial_scan_never_invalidates_unseen_items(repository):
    result = await service.scan(source_id="source-1", mode="incremental", list_children_error=True)
    assert result.invalidated_count == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_scan_service.py backend/test/unit/repositories/test_feishu_knowledge_repository.py`

Expected: FAIL because repository and scanner are absent.

- [ ] **Step 3: 实现 repository 事务接口**

提供 `get_or_create_source`、`start_sync_run`、`upsert_source_item`、`find_current_version`、`create_material_version`、`mark_seen_items`、`mark_source_invalid`、`append_event` 和 `get_source_summary`。所有写操作使用显式 async session 事务；状态更新带当前状态条件，避免 API/worker 并发覆盖。

- [ ] **Step 4: 实现递归扫描服务**

从根 node token 开始递归读取子节点；页面正文和附件生成独立 `source_item`；下载前先按 revision/更新时间/哈希判定是否变化；支持附件扩展名映射；音视频只创建 `unsupported` 版本；完整成功扫描结束后再计算未见对象，局部错误直接将批次置为失败且不做失效标记。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_scan_service.py backend/test/unit/repositories/test_feishu_knowledge_repository.py`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/package/yuxi/integrations/feishu/service.py backend/package/yuxi/repositories/feishu_knowledge_repository.py backend/test/unit/integrations/test_feishu_scan_service.py backend/test/unit/repositories/test_feishu_knowledge_repository.py
git commit -m "feat: add Feishu recursive scan and version tracking"
```

### Task 4: 接入 Tasker、素材归档和人工审核/发布状态机

**Files:**
- Create: `backend/server/routers/feishu_knowledge_router.py`
- Create: `backend/test/unit/routers/test_feishu_knowledge_router.py`
- Create: `backend/test/integration/api/test_feishu_knowledge_router.py`
- Modify: `backend/server/routers/__init__.py`
- Modify: `backend/server/utils/lifespan.py`

- [ ] **Step 1: 写 API 和状态流转失败测试**

覆盖管理员权限、数据源创建、连接检查、全量/增量任务唯一性、批次查询、素材筛选、审核通过/驳回必须填写原因、来源失效确认下架和 100 条批量限制。

```python
async def test_approve_requires_reviewed_material(test_client, admin_headers, feishu_fixture):
    response = await test_client.post(
        f"/api/feishu-knowledge/materials/{feishu_fixture.version_id}/approve",
        headers=admin_headers,
    )
    assert response.status_code == 409

async def test_reject_requires_reason(test_client, admin_headers, feishu_fixture):
    response = await test_client.post(
        f"/api/feishu-knowledge/materials/{feishu_fixture.version_id}/reject",
        json={"reason": ""},
        headers=admin_headers,
    )
    assert response.status_code == 422
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/routers/test_feishu_knowledge_router.py`

Expected: FAIL because router and endpoints do not exist.

- [ ] **Step 3: 实现数据源与扫描 API**

新增管理员接口：

```text
GET  /api/feishu-knowledge/sources
POST /api/feishu-knowledge/sources
POST /api/feishu-knowledge/sources/{source_id}/check
POST /api/feishu-knowledge/sources/{source_id}/scan
GET  /api/feishu-knowledge/sources/{source_id}/runs
GET  /api/feishu-knowledge/runs/{run_id}
```

`scan` 仅接受 `full` 或 `incremental`，通过 `tasker.enqueue_unique_by_payload` 保证同一数据源只有一个活跃批次；返回 `task_id` 和 `run_id`。

- [ ] **Step 4: 实现素材、审核和来源失效 API**

新增接口：

```text
GET  /api/feishu-knowledge/sources/{source_id}/materials
GET  /api/feishu-knowledge/materials/{version_id}
GET  /api/feishu-knowledge/materials/{version_id}/events
POST /api/feishu-knowledge/materials/{version_id}/approve
POST /api/feishu-knowledge/materials/{version_id}/reject
POST /api/feishu-knowledge/materials/{version_id}/retry
POST /api/feishu-knowledge/materials/batch-action
POST /api/feishu-knowledge/materials/{version_id}/confirm-removal
```

审核通过只改变审核状态并排队发布；发布成功后在一个事务内切换 `active_version_id`，旧版本标记 replaced；发布失败保留旧 active 版本并记录错误。

- [ ] **Step 5: 实现素材归档与发布适配器**

扫描任务把页面 Markdown 和附件原文件写入 MinIO，并为支持类型调用现有 `files/upload`、`documents/add`、解析和索引内部服务；不通过 HTTP 自调用，使用服务层函数避免重复鉴权和网络开销。发布器必须传递 `source_url`、Wiki 路径、版本和页码到 `processing_params`，保证现有检索引用可展示飞书来源。

- [ ] **Step 6: 注册路由并验证启动**

在 `server/routers/__init__.py` 注册；确认 `lifespan` 只创建表和启动现有 Tasker，不增加定时任务。启动时不得扫描飞书或调用外部写接口。

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/routers/test_feishu_knowledge_router.py`

Then: `uv run --project backend --no-sync --no-dev pytest -q backend/test/integration/api/test_feishu_knowledge_router.py`

Expected: PASS；没有配置真实飞书凭据时，真实网络扫描测试必须 skip，不得失败。

- [ ] **Step 8: 提交**

```bash
git add backend/server/routers/feishu_knowledge_router.py backend/server/routers/__init__.py backend/server/utils/lifespan.py backend/test/unit/routers/test_feishu_knowledge_router.py backend/test/integration/api/test_feishu_knowledge_router.py
git commit -m "feat: add Feishu sync and approval APIs"
```

### Task 5: 建立正式知识库并实现管理员工作台

**Files:**
- Create: `web/src/apis/feishu_knowledge_api.js`
- Create: `web/src/views/FeishuKnowledgeView.vue`
- Create: `web/src/components/feishu/FeishuSyncRunsTable.vue`
- Create: `web/src/components/feishu/FeishuMaterialTable.vue`
- Create: `web/src/components/feishu/FeishuMaterialDetailDrawer.vue`
- Create: `web/src/views/FeishuKnowledgeView.spec.js`
- Modify: `web/src/router/index.js`
- Modify: `web/src/layouts/AppLayout.vue`

- [ ] **Step 1: 写 API 模块和 UI 状态失败测试**

测试 API 方法映射、批量上限、扫描按钮 loading、审核确认、错误提示和轮询任务状态。

- [ ] **Step 2: 运行前端测试确认失败**

Run: `pnpm --dir web exec vitest run src/views/FeishuKnowledgeView.spec.js`

Expected: FAIL because the page and API module do not exist.

- [ ] **Step 3: 实现 API 封装**

统一复用现有 `apiAdminGet/apiAdminPost`；将后端错误 `detail` 转换为中文提示；批量 action 在前端先拒绝超过 100 条，后端仍必须二次校验。

- [ ] **Step 4: 实现数据源与批次视图**

页面显示根节点、目标知识库、最近批次、统计和全量/增量按钮；任务执行期间禁用同一数据源的另一个扫描按钮，定时刷新批次状态，不增加自动轮询以外的后台调度。

- [ ] **Step 5: 实现素材表和详情抽屉**

支持状态/类型/目录/时间筛选；详情抽屉显示飞书来源、原文链接、Markdown、Chunks 和事件时间线；审核通过/驳回/重试/确认下架均要求明确确认，驳回必须输入原因；音视频显示“暂不支持加工”。

- [ ] **Step 6: 接入管理员导航与路由**

增加 `/feishu-knowledge`，要求登录且管理员权限；非管理员按现有路由守卫回到 `/agent`；不改动现有知识库详情页布局。

- [ ] **Step 7: 运行测试确认通过**

Run: `pnpm --dir web exec vitest run src/views/FeishuKnowledgeView.spec.js`

Then: `pnpm --dir web build`

Expected: Vitest PASS，前端构建 exit 0。

- [ ] **Step 8: 提交**

```bash
git add web/src/apis/feishu_knowledge_api.js web/src/views/FeishuKnowledgeView.vue web/src/components/feishu web/src/router/index.js web/src/layouts/AppLayout.vue web/src/views/FeishuKnowledgeView.spec.js
git commit -m "feat: add Feishu knowledge processing workbench"
```

### Task 6: 端到端验收与部署配置

**Files:**
- Create: `backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py`
- Modify: `docs/advanced/deployment.md`
- Modify: `docs/implementation/acceptance-log.md`
- Modify: `.env.example`

- [ ] **Step 1: 写离线端到端测试夹具**

用 fake Feishu transport 提供三层页面、PDF/TXT/PNG 附件和音视频；用本地 MinIO/Milvus 测试 fixture 验证全量扫描、待审核、驳回、通过、发布和检索来源。

- [ ] **Step 2: 写增量和原子替换测试**

第一次全量发布 v1；第二次只修改一个页面，确认未变化素材不创建新版本，旧 v1 在新版本审核前仍可检索；新 v2 入库成功后 active 切换，旧向量不再召回。

- [ ] **Step 3: 写异常安全测试**

验证权限失败不会来源失效、局部扫描失败不会批量下架、429 重试后可继续、入库失败保留旧 active、音视频不进入失败计数、日志不含 token。

- [ ] **Step 4: 运行完整验证**

Run:

```bash
uv run --project backend --no-sync --no-dev pytest -q backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py
uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations backend/test/unit/repositories/test_feishu_knowledge_repository.py backend/test/unit/routers/test_feishu_knowledge_router.py
pnpm --dir web exec vitest run src/views/FeishuKnowledgeView.spec.js
pnpm --dir web build
git diff --check
```

Expected: 全部测试通过，前端构建 exit 0，diff 无空白错误。

- [ ] **Step 5: 使用真实飞书凭据做受控验收**

仅在用户已在本机配置 `FEISHU_ACCESS_TOKEN`（或数据源指定的安全变量名）后执行：连接检查 → 小范围权限验证 → 全量扫描；确认根节点标题、子页面数量和附件类型后再允许发布。测试过程不输出 token，不把真实企业内容复制进测试材料或提交。

- [ ] **Step 6: 验收重启持久化**

只重建 API/worker，不重置 PostgreSQL、Redis、MinIO、Milvus 命名卷；重启后核对同步批次、素材版本、审核结果和 active 版本仍存在。

- [ ] **Step 7: 更新验收记录**

阶段二所有验收项均有真实证据后，才在 `acceptance-log.md` 将阶段二改为 PASS；若飞书权限、接口或内容类型被阻塞，保持 RUNNING 并记录具体阻塞原因。

- [ ] **Step 8: 提交文档与验收结果**

```bash
git add backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py docs/advanced/deployment.md docs/implementation/acceptance-log.md .env.example
git commit -m "test: verify Feishu knowledge pipeline"
```

---

## 计划自检

- 规格覆盖：扫描范围、五类状态表、版本判断、人工审核、原子替换、来源失效保护、工作台、引用和安全均有对应任务。
- 占位符检查：无 `TODO`、`TBD` 或未定义的“后续补充”步骤。
- 类型一致性：`source_id`、`item_id`、`version_id` 在模型、repository、API 和 UI 中保持同名；状态名统一使用 `processing_status` 与 `review_status`。
- 数据安全：真实凭据只在部署前配置；测试使用 fake transport；日志和提交中不包含密钥或企业正文。
- 破坏性操作：计划不包含 `make reset`、`docker compose down -v`、删除命名卷或飞书写操作。

