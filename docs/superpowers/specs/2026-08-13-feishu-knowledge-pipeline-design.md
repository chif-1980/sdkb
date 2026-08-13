# 阶段二：飞书 Wiki 知识加工流水线设计

## 1. 目标与边界

阶段二在 Yuxi 内建立一条“飞书 Wiki 只读同步 → 原始素材归档 → 解析与人工审核 → 正式知识发布”的流水线。

已确认范围：

- 飞书根节点：`https://quickdone.feishu.cn/wiki/VO95wRtWri5XoNkKqU0cLjQ3nqc`
- 递归读取该 Wiki 节点的子页面、目录和可访问附件。
- 支持 Wiki 页面以及 PDF、DOCX、PPTX、XLSX、TXT、图片附件。
- 音视频首版仅登记来源和“暂不支持加工”状态，不做转写、说话人识别或入库。
- 新建独立正式知识库：`Quickdone 企业知识库`，与阶段一验收库隔离。
- PostgreSQL 是同步、加工、审核和发布状态的唯一事实源。
- MinIO 保存原始素材和解析产物；Milvus 保存审核通过后的向量知识。
- 飞书始终只读，不写回页面、不写回多维表格。
- 首次由管理员手动触发全量扫描；后续由管理员手动触发增量同步；首版不做定时任务。

明确不包含：音视频转写、飞书写回、多维表格状态同步、复杂权限映射、自动下架。

## 2. 总体架构

```text
飞书 Wiki（只读）
    ↓ 递归扫描
同步批次与素材版本（PostgreSQL）
    ↓ 原始文件归档
MinIO 原始区 / 解析区
    ↓ 解析、OCR、分块、人工审核
Quickdone 企业知识库（PostgreSQL + Milvus）
    ↓
RAG 检索与飞书来源引用
```

新增模块：

1. **飞书数据源**：保存根节点、同步范围、目标知识库和凭据变量名；不保存凭据正文。
2. **飞书扫描器**：递归读取页面、附件、标题、路径、更新时间、revision 和来源 URL。
3. **素材与版本库**：在 PostgreSQL 保存来源对象、版本、哈希、处理状态和审计事件。
4. **知识加工工作台**：提供数据源、同步批次和素材加工视图，以及审核、重试和下架确认操作。
5. **知识发布器**：审核通过后调用现有解析、分块和 `BAAI/bge-m3` 向量化链路，完成版本发布。

## 3. 数据模型

### 3.1 `feishu_sources`

表示一个飞书知识源：

- `id`、`name`、`wiki_root_token`、`wiki_root_url`
- `target_kb_id`
- `credential_env_name`（仅变量名）
- `enabled`、`created_by`、`created_at`、`updated_at`

### 3.2 `feishu_sync_runs`

表示一次全量或增量扫描：

- `id`、`source_id`、`run_type`、`status`
- `started_at`、`finished_at`、`operator_id`
- `scanned_count`、`new_count`、`changed_count`、`unchanged_count`
- `unsupported_count`、`failed_count`、`invalidated_count`
- `error_summary`

同一数据源同一时间只允许一个扫描任务运行。

### 3.3 `feishu_source_items`

表示稳定的飞书对象：

- Wiki 页面使用 `space_id + node_token` 唯一标识；附件使用 `file_token` 唯一标识。
- `source_id`、`item_key`、`item_type`（page、attachment、audio、video）
- `title`、`parent_item_key`、`path_text`、`source_url`
- `last_seen_at`、`source_updated_at`、`source_validity`
- `active_version_id`、`created_at`、`updated_at`

### 3.4 `feishu_material_versions`

保存每个素材版本：

- `id`、`item_id`、`revision`、`content_hash`
- `source_object_path`、`parsed_object_path`
- `processing_status`、`processing_params`
- `error_code`、`error_message`、`retry_count`
- `review_status`、`reviewer_id`、`reviewed_at`、`review_comment`
- `yuxi_file_id`、`chunk_count`、`token_count`
- `published_at`、`replaced_at`

### 3.5 `feishu_processing_events`

追加式审计记录：

- `id`、`source_id`、`item_id`、`version_id`
- `event_type`、`from_status`、`to_status`
- `operator_id`、`message`、`payload_json`、`created_at`

不得在事件或日志中写入凭据正文。

## 4. 状态机与版本规则

### 4.1 素材处理状态

```text
已发现 → 同步中 → 已同步 → 解析中 → 待审核 → 已通过 → 入库中 → 已发布
             ↘ 同步失败       ↘ 解析失败       ↘ 已驳回       ↘ 入库失败
```

音视频使用独立的 `暂不支持加工` 状态，不记为失败。

### 4.2 增量判断

依次使用 `飞书 revision → 更新时间 → 内容哈希` 判断版本。内容一致时只更新最近检查时间，不创建新版本。

### 4.3 原子替换

- 旧版本在新版本审核、解析和入库完成前继续保持 active 并参与检索。
- 新版本全部 Chunk 向量化成功后，在 PostgreSQL 事务内切换 `active_version_id`。
- 旧向量随后异步清理；清理失败不得影响当前 active 版本。

### 4.4 来源失效

- 只有完整且成功的目录扫描，才能把未发现对象标记为“来源失效”。
- 局部扫描失败、权限异常或超时不得批量标记失效。
- 来源失效时保留当前正式版本并继续检索，等待管理员确认下架。

## 5. 工作台与操作流程

### 5.1 数据源页

展示数据源、Wiki 根节点、目标知识库、最近全量/增量同步时间，以及素材总数、待审核数、失败数、来源失效数。

操作：权限检查、全量扫描、增量同步。

### 5.2 同步批次页

展示批次类型、操作者、时间、扫描/新增/变更/跳过/失败/来源失效数量、进度和错误摘要，并可查看批次素材。

### 5.3 素材加工页

按状态、类型、目录和更新时间筛选，展示飞书标题、路径、版本、状态、正式版本、来源链接、错误原因和操作历史。

管理员操作：查看原文、预览 Markdown、查看 Chunks、重新同步、重新解析、审核通过、驳回、重试发布、确认下架。

首版批量操作上限 100 条：批量通过、批量驳回、批量重试解析、批量确认下架。

### 5.4 审核门槛

管理员必须对照飞书原文和解析 Markdown，检查标题、正文、表格、图片 OCR、来源信息后才能通过。驳回必须填写原因，版本保留但不得进入正式知识库。

## 6. 来源引用

RAG 结果保留飞书标题、Wiki 路径、原始链接、素材类型和版本时间；PDF 尽量保留页码；附件引用同时显示附件名和所属 Wiki 页面。点击来源优先打开飞书原始页面；权限不足时明确提示无法打开，但保留引用信息。

## 7. 错误处理与安全

- 飞书鉴权失败、权限不足、节点不存在、请求限流和网络超时都要转换为可读错误并记录事件。
- 同步任务支持安全重试；不可重试错误进入人工处理队列。
- 上传、解析、入库均应幂等，内容哈希和稳定对象标识用于去重。
- API/worker 共享 PostgreSQL 状态，不依赖进程内队列状态。
- 飞书凭据只从安全配置或环境变量读取，任何响应、日志和事件均不得暴露密钥。

## 8. 验收标准

1. 递归扫描指定 Wiki 根节点并识别新增、修改、未变化和来源失效素材。
2. 支持的页面与附件能够落入 MinIO、解析并进入待审核。
3. 音视频能登记为暂不支持加工，不误报为失败。
4. 审核通过后发布到独立的 `Quickdone 企业知识库`。
5. 新版本发布后原子替换旧版本，替换期间检索不中断。
6. 所有失败可在工作台查看原因并重试。
7. RAG 结果可追溯到飞书标题、路径和原始链接。
8. API/worker 重启后同步、审核和发布状态保持一致。

