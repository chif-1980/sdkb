# 知识审核 P0 实施规格

## 1. 文档目的

本文定义 Yuxi“知识加工”页面中知识审核 P0 阶段的落地规格，作为数据库迁移、后端接口、前端改造和自动化测试的共同依据。

P0 只解决以下问题：

- 把一份来源版本的一组变化组织成一个审核包。
- 让审核人按业务场景处理审核项，不再选择内部技术动作。
- 建立“需要修改 → 飞书源资料更新 → 重新审核”的闭环。
- 将下载、解析、OCR、正文缺失等加工异常从知识审核中分离。
- 保留完整审核草稿、转交记录和状态历史。

P0 不改变飞书只读原则：审核人员不能通过后台覆盖飞书原文中的事实内容。

## 2. 范围与非目标

### 2.1 P0 范围

1. 新增审核包、审核项、资料修改任务三类持久化结构。
2. 将现有一份素材版本一个审核任务迁移为一份素材版本一个审核包、至少一个审核项。
3. 统一审核状态及其页面显示语义。
4. 新增审核包读取、草稿、提交和转交接口。
5. 调整现有 `FeishuReviewWorkspace`，按审核类型显示场景化操作。
6. 复用现有素材发布、Yuxi 入库、跨文档关系和处理事件能力。
7. 保持已有正式知识和旧正式版本在新版本审核期间可用。

### 2.2 P0 非目标

以下能力明确放到 P1 或以后，不在 P0 中顺带实现：

- 将正式知识从飞书文档升级为独立逻辑知识。
- 建立稳定来源片段、原子知识和片段级重复关系。
- 三份 PPT 中相同“公司简介”自动合并为一条正式知识。
- RAG 按逻辑知识聚合、去重和冲突输出。
- 重写跨文档比较算法或增加大模型深度比较。
- 自动识别资料责任人、飞书消息通知和催办。
- 点赞点踩反馈自动生成有效性复核任务。
- 音视频转写和审核。

P0 的审核项先允许指向整份素材版本或现有跨文档关系。P1 增加来源片段后，审核项继续复用，不重新设计审核状态和接口。

## 3. 核心业务原则

### 3.1 三路分流

素材完成扫描后必须先分流，不能全部进入人工审核：

```text
扫描完成
  ├─ 自动处理
  │    ├─ 目录节点
  │    ├─ 内容未变化
  │    └─ 其他可确定跳过的情况
  ├─ 加工异常
  │    ├─ 下载失败
  │    ├─ 权限不足
  │    ├─ 解析/OCR 失败
  │    └─ 非目录节点但没有可审核正文
  └─ 人工知识审核
       ├─ 新增知识
       ├─ 知识变更
       ├─ 冲突裁决
       └─ 有效性复核
```

目录节点继续使用现有 `skip_reason=directory` 规则自动跳过。非目录节点只有标题、没有正文时进入素材加工异常，不创建审核包。

### 3.2 来源与事实边界

- 飞书是原始事实来源，后台只读。
- 审核人可以修正 AI 提取的适用范围、问题标签等结构化元数据。
- 审核人不能脱离来源直接修改事实正文。
- 事实错误应创建资料修改任务，由资料责任人在飞书修改。
- 新飞书版本到达后重新扫描和审核，旧正式知识在新版发布前继续生效。

### 3.3 审核单位

- 列表单位：审核包，一般对应一个飞书素材版本触发的一组变化。
- 决策单位：审核项，表示一个需要人工判断的新增、变更、冲突或有效性问题。
- P0 迁移后的历史任务默认一包一项。
- P1 接入原子知识后，一个审核包可以包含多个片段级审核项。

### 3.4 内部动作自动推导

`CREATE`、`UPDATE`、`KEEP_CURRENT`、`ARCHIVE`、`MARK_DUPLICATE`、`SPLIT_BY_SCOPE` 等内部动作继续保留在后端，但不得作为通用下拉框展示给审核人。

页面提交业务结果 `outcome`，后端根据审核类型和结果推导内部动作，并在审核记录中同时保存业务结果和推导动作，便于审计。

## 4. 数据模型

P0 新增三张主表。现有 `feishu_governance_reviews` 暂不删除，用作迁移来源和回滚保障。

### 4.1 `feishu_review_packages`

表示一次审核工作的业务容器。

| 字段 | 类型 | 约束与说明 |
| --- | --- | --- |
| `id` | bigint | 主键 |
| `package_id` | varchar(64) | 稳定业务 ID，唯一 |
| `package_key` | varchar(256) | 幂等键，唯一 |
| `source_id` | varchar(64) | 飞书数据源 ID，非空 |
| `source_item_id` | varchar(64) | 飞书稳定对象 ID，可空；生命周期任务可能不依赖新素材 |
| `source_version_id` | varchar(64) | 触发审核的素材版本，可空 |
| `trigger_type` | varchar(32) | `SOURCE_VERSION`、`LIFECYCLE`、`FEEDBACK` |
| `title_snapshot` | varchar(512) | 创建审核包时的标题快照 |
| `path_snapshot` | varchar(2048) | 飞书路径快照 |
| `source_url_snapshot` | varchar(2048) | 飞书原文链接快照 |
| `workflow_status` | varchar(40) | 见第 5 节 |
| `assignee_id` | varchar(64) | 当前负责人 |
| `risk_level` | varchar(16) | `LOW`、`MEDIUM`、`HIGH` |
| `draft_json` | json | 页面草稿，仅保存未提交内容 |
| `lock_version` | integer | 乐观锁版本，默认 1 |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 更新时间 |
| `completed_at` | timestamptz | 完成时间，可空 |

幂等规则：

- 来源版本触发：`package_key = source-version:{source_version_id}`。
- 生命周期触发：`package_key = lifecycle:{knowledge_id}:{cycle_key}`。
- 同一个 `package_key` 重复执行只更新仍未完成的审核包，不重复创建。

### 4.2 `feishu_review_items`

表示审核包中的一个决策项。

| 字段 | 类型 | 约束与说明 |
| --- | --- | --- |
| `id` | bigint | 主键 |
| `review_item_id` | varchar(64) | 稳定业务 ID，唯一 |
| `package_id` | varchar(64) | 审核包外键 |
| `candidate_key` | varchar(256) | 包内稳定幂等键 |
| `review_type` | varchar(24) | `NEW`、`UPDATE`、`CONFLICT`、`STALE` |
| `subject_type` | varchar(32) | P0 支持 `MATERIAL_VERSION`、`COMPARISON_RELATION`、`KNOWLEDGE` |
| `subject_id` | varchar(64) | 被审核对象 ID |
| `title` | varchar(512) | 审核项标题 |
| `summary` | text | AI 或规则生成的变化摘要 |
| `subject_locator_json` | json | 页码、章节、现有 Chunk 等定位信息；P0 可为空 |
| `evidence_json` | json | 来源、比较关系和差异证据快照 |
| `relation_ids` | json | 仅与本审核项相关的跨文档关系 ID |
| `problem_tags` | json | 知识问题标签，不放加工异常 |
| `applicability_scope` | json | AI 提取后由人工确认的适用范围 |
| `item_status` | varchar(40) | 见第 5 节 |
| `outcome` | varchar(48) | 审核人选择的业务结果 |
| `internal_action` | varchar(32) | 后端推导的内部动作 |
| `decision_comment` | text | 裁决依据或修改要求 |
| `decision_payload` | json | 提交时完整快照 |
| `decided_by` | varchar(64) | 决策人 |
| `decided_at` | timestamptz | 决策时间 |
| `reopened_from_item_id` | varchar(64) | 新版本重新审核时关联上一轮审核项 |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 更新时间 |

唯一约束：`(package_id, candidate_key)`。

P0 的 `candidate_key` 规则：

- 普通新增或变更：`material:{source_version_id}`。
- 跨文档冲突：`relation:{relation_id}`。
- 有效性复核：`knowledge:{knowledge_id}:{cycle_key}`。

P1 引入来源片段后增加 `segment:{segment_id}`，不改变现有表结构和 API 主语义。

### 4.3 `feishu_source_change_requests`

表示审核人要求资料责任人修改飞书原文的任务。

| 字段 | 类型 | 约束与说明 |
| --- | --- | --- |
| `id` | bigint | 主键 |
| `change_request_id` | varchar(64) | 稳定业务 ID，唯一 |
| `review_item_id` | varchar(64) | 发起修改的审核项 |
| `source_item_id` | varchar(64) | 要修改的飞书稳定对象 |
| `requested_version_id` | varchar(64) | 发起修改时的来源版本 |
| `source_url` | varchar(2048) | 飞书原文入口 |
| `status` | varchar(40) | `OPEN`、`NEW_VERSION_RECEIVED`、`FULFILLED`、`CANCELLED` |
| `request_text` | text | 必填，说明问题和修改要求 |
| `responsible_user_id` | varchar(128) | P0 手工选择或填写，可空 |
| `responsible_user_name` | varchar(255) | 显示名称，可空 |
| `round_number` | integer | 同一问题修改轮次，从 1 开始 |
| `received_version_id` | varchar(64) | 系统检测到的新来源版本 |
| `created_by` | varchar(64) | 发起人 |
| `created_at` | timestamptz | 发起时间 |
| `updated_at` | timestamptz | 更新时间 |
| `resolved_at` | timestamptz | 完成或取消时间 |

同一审核项同一轮只允许一个未取消的修改任务。

### 4.4 复用现有事件表

继续使用 `feishu_processing_events` 保存追加式历史，在 `payload_json` 中写入：

```json
{
  "package_id": "review-package-...",
  "review_item_id": "review-item-...",
  "change_request_id": "change-request-...",
  "outcome": "REQUEST_SOURCE_CHANGE",
  "internal_action": null
}
```

新增事件类型：

- `review_package_created`
- `review_draft_saved`
- `review_item_decided`
- `review_package_transferred`
- `source_change_requested`
- `source_change_version_received`
- `review_item_reopened`
- `review_package_completed`
- `review_package_invalidated`

事件只追加，不覆盖历史记录。

## 5. 状态机

### 5.1 审核包持久化状态

```text
OPEN
  ├─ 所有审核项完成 ─────────────→ COMPLETED
  ├─ 存在待修改审核项 ───────────→ WAITING_SOURCE_CHANGE
  ├─ 存在待业务确认审核项 ───────→ WAITING_BUSINESS_CONFIRMATION
  └─ 来源版本被撤销或任务被替代 ─→ INVALIDATED

WAITING_SOURCE_CHANGE
  ├─ 检测到实质变化的新飞书版本 → COMPLETED（旧包）
  │                                + OPEN（新版本审核包）
  └─ 取消修改任务且无其他待办 ───→ COMPLETED

WAITING_BUSINESS_CONFIRMATION
  ├─ 收到确认并形成裁决 ─────────→ COMPLETED
  └─ 转为资料修改 ───────────────→ WAITING_SOURCE_CHANGE
```

转交不是审核包终态。转交只改变 `assignee_id` 并写事件：

- 新负责人看到“待我处理”。
- 原负责人可在历史筛选中看到“已转交”。
- 不使用 `TRANSFERRED` 作为阻止继续处理的持久化状态。

### 5.2 页面业务状态

| 页面显示 | 计算规则 |
| --- | --- |
| 待我处理 | `workflow_status=OPEN` 且当前用户为负责人 |
| 等待资料修改 | `workflow_status=WAITING_SOURCE_CHANGE` |
| 等待业务确认 | `workflow_status=WAITING_BUSINESS_CONFIRMATION` |
| 已转交 | 当前用户是最近一次转交事件的转出人，且不再是负责人 |
| 已完成 | `workflow_status=COMPLETED` |
| 已失效 | `workflow_status=INVALIDATED` |

### 5.3 审核项状态

```text
PENDING
  ├─ 形成最终裁决 ───────────→ DECIDED
  ├─ 要求修改飞书原文 ───────→ WAITING_SOURCE_CHANGE
  ├─ 等待业务负责人确认 ─────→ WAITING_BUSINESS_CONFIRMATION
  └─ 来源或候选失效 ─────────→ INVALIDATED

WAITING_SOURCE_CHANGE
  └─ 检测到实质变化的新版本 ─→ SOURCE_UPDATED
                                  + 新审核项 PENDING

WAITING_BUSINESS_CONFIRMATION
  ├─ 形成裁决 ───────────────→ DECIDED
  └─ 要求修改原文 ───────────→ WAITING_SOURCE_CHANGE
```

### 5.4 “需要修改”闭环

1. 审核人选择“退回修改”，填写修改要求。
2. 后端在同一事务中：
   - 把审核项置为 `WAITING_SOURCE_CHANGE`。
   - 创建 `OPEN` 的资料修改任务。
   - 重新计算审核包状态。
   - 写入处理事件。
3. 资料责任人在飞书修改原文。
4. 后续扫描发现同一 `source_item_id` 出现新 `source_version_id`。
5. 只有内容 Hash 实质变化时才触发重开；仅更新时间变化不重开。
6. 原修改任务置为 `NEW_VERSION_RECEIVED`，原审核项置为 `SOURCE_UPDATED`。
7. 为新版本创建新审核包，并通过 `reopened_from_item_id` 保留上下文。
8. 新审核通过后，修改任务置为 `FULFILLED`；如仍不合格，可发起下一轮修改。

## 6. 场景化审核结果

### 6.1 新增知识 `NEW`

| 页面操作 | `outcome` | 后端动作 |
| --- | --- | --- |
| 发布 | `PUBLISH` | 推导 `CREATE`，调用现有审核通过与发布链路 |
| 退回修改 | `REQUEST_SOURCE_CHANGE` | 不发布，创建资料修改任务 |
| 不纳入知识库 | `EXCLUDE` | 推导 `ARCHIVE`，保留来源和审核记录 |
| 转交 | 独立转交操作 | 更换审核包负责人，不形成知识裁决 |

### 6.2 知识变更 `UPDATE`

| 页面操作 | `outcome` | 后端动作 |
| --- | --- | --- |
| 采用新版 | `ADOPT_NEW_VERSION` | 推导 `UPDATE`，发布后原子替换旧版本 |
| 保留当前 | `KEEP_CURRENT` | 推导 `KEEP_CURRENT`，候选不发布 |
| 按适用范围拆分 | `SPLIT_SCOPE` | 推导 `SPLIT_BY_SCOPE`，必须提交完整适用范围 |
| 退回修改 | `REQUEST_SOURCE_CHANGE` | 创建资料修改任务 |
| 转交 | 独立转交操作 | 更换审核包负责人 |

### 6.3 冲突裁决 `CONFLICT`

| 页面操作 | `outcome` | 后端动作 |
| --- | --- | --- |
| 保留当前 | `KEEP_CURRENT` | 推导 `KEEP_CURRENT`，仅解决本审核项关联关系 |
| 采用新版 | `ADOPT_NEW_VERSION` | 推导 `UPDATE`，仅解决本审核项关联关系 |
| 拆分适用范围 | `SPLIT_SCOPE` | 推导 `SPLIT_BY_SCOPE`，仅解决本审核项关联关系 |
| 等待业务确认 | `WAIT_BUSINESS_CONFIRMATION` | 暂不发布，进入等待确认 |
| 转交 | 独立转交操作 | 更换审核包负责人 |

冲突审核项不得参与批量裁决。

### 6.4 有效性复核 `STALE`

| 页面操作 | `outcome` | 后端动作 |
| --- | --- | --- |
| 仍然有效 | `CONFIRM_VALID` | 推导 `KEEP_CURRENT`，刷新复核时间 |
| 要求更新 | `REQUEST_SOURCE_CHANGE` | 创建资料修改任务 |
| 补充来源 | `REQUEST_SUPPORTING_SOURCE` | 进入等待资料修改或补充状态 |
| 归档 | `ARCHIVE` | 推导 `ARCHIVE`，从正式索引移除 |
| 不是知识问题 | `DISMISS` | 关闭本次复核，记录原因，不改变正文 |
| 转交 | 独立转交操作 | 更换审核包负责人 |

## 7. API 规格

新接口与现有 `/api/governance` 保持同一权限边界，只允许知识管理员访问。

### 7.1 审核包列表

```text
GET /api/governance/review-packages
```

查询参数：

- `source_id`：必填。
- `view`：`mine`、`all`、`transferred_by_me`，默认 `mine`。
- `workflow_status`：可重复。
- `review_type`：可重复。
- `problem_tag`：可选。
- `risk_level`：可选。
- `page`、`page_size`。

响应必须直接返回各状态总数，避免进入页面时待审核数量先显示为 0：

```json
{
  "items": [],
  "total": 0,
  "counts": {
    "mine": 0,
    "waiting_source_change": 0,
    "waiting_business_confirmation": 0,
    "completed": 0
  }
}
```

### 7.2 审核包详情

```text
GET /api/governance/review-packages/{package_id}
```

响应包含：

- 审核包信息和当前负责人。
- 来源标题、路径、飞书链接和版本。
- 审核项列表及各自允许的 `outcomes`。
- 内容预览定位信息。
- 仅与审核项相关的跨文档证据。
- 当前草稿和 `lock_version`。
- 修改任务和事件时间线。

允许的操作由后端返回，前端不得自行猜测：

```json
{
  "review_item_id": "review-item-1",
  "review_type": "CONFLICT",
  "allowed_outcomes": [
    "KEEP_CURRENT",
    "ADOPT_NEW_VERSION",
    "SPLIT_SCOPE",
    "WAIT_BUSINESS_CONFIRMATION"
  ]
}
```

### 7.3 保存草稿

```text
PATCH /api/governance/review-packages/{package_id}/draft
```

请求：

```json
{
  "lock_version": 3,
  "draft": {
    "review_item_id": "review-item-1",
    "outcome": "REQUEST_SOURCE_CHANGE",
    "decision_comment": "请在飞书原文补充适用版本和生效时间",
    "applicability_scope": {}
  }
}
```

要求：

- 成功后增加 `lock_version`。
- 版本冲突返回 HTTP 409 和最新草稿，不静默覆盖其他审核人的内容。
- 草稿不改变审核状态、不触发发布。

### 7.4 提交审核结果

```text
POST /api/governance/review-packages/{package_id}/resolve
```

请求：

```json
{
  "lock_version": 4,
  "decisions": [
    {
      "review_item_id": "review-item-1",
      "outcome": "REQUEST_SOURCE_CHANGE",
      "problem_tags": ["MISSING_SCOPE"],
      "decision_comment": "请补充产品版本及生效时间",
      "applicability_scope": {},
      "responsible_user_id": "optional-user-id",
      "responsible_user_name": "资料责任人"
    }
  ]
}
```

后端必须：

1. 校验当前用户是负责人。
2. 校验 `lock_version`。
3. 校验每个 `outcome` 属于当前审核类型的允许集合。
4. 推导并保存 `internal_action`。
5. 只处理本次提交的审核项及其 `relation_ids`。
6. 需要修改时事务内创建资料修改任务。
7. 发布时继续使用现有发布队列，返回 `publish_task_id`。
8. 重新计算审核包状态并写入事件。
9. 同一个请求重复提交不得重复发布或重复创建修改任务。

### 7.5 转交审核包

```text
POST /api/governance/review-packages/{package_id}/transfer
```

请求：

```json
{
  "lock_version": 4,
  "assignee_id": "admin-user-id",
  "comment": "请由产品负责人确认版本范围"
}
```

转交成功后原负责人不能继续提交；新负责人进入页面时看到“待我处理”。

P0 按审核包整体转交，不做单个审核项拆包转交，避免过度复杂。

### 7.6 资料修改任务

```text
GET  /api/governance/source-change-requests
GET  /api/governance/source-change-requests/{change_request_id}
POST /api/governance/source-change-requests/{change_request_id}/cancel
```

P0 不提供后台直接修改飞书正文的接口。

### 7.7 旧接口兼容

现有接口：

```text
GET  /api/governance/reviews
GET  /api/governance/reviews/{review_id}
POST /api/governance/reviews/{review_id}/resolve
```

处理策略：

1. 首次部署只增加新表和新接口，旧页面保持可运行。
2. 完成数据回填后切换新页面读取新接口。
3. UI 切换后，旧写接口改为兼容适配或只读，禁止新旧模型同时独立写入。
4. 稳定一个发布周期后再决定是否删除旧接口和旧表。

## 8. 页面交互规格

改造位置仍是现有 Yuxi“知识加工 → 待审核”，不新增割裂的后台系统。

### 8.1 页面结构

```text
左侧：审核包队列
  ├─ 标题、来源路径
  ├─ 新增/变更/冲突/复核数量
  ├─ 风险、负责人、业务状态
  └─ 筛选与搜索

中间：审核内容与证据
  ├─ 审核项导航
  ├─ 待审核正文/变化摘要
  ├─ 飞书原文入口
  ├─ 跨文档证据
  └─ 历史处理时间线

右侧：当前审核项决策
  ├─ 场景化业务按钮
  ├─ 问题标签
  ├─ 适用范围确认
  ├─ 审核意见/修改要求
  └─ 保存草稿、提交
```

### 8.2 队列

- 默认进入“待我处理”。
- 一行代表一个审核包，不是一条跨文档关系，也不是一个 Chunk。
- 显示“共 4 项：新增 2、变更 1、冲突 1”。
- 冲突、高风险和等待时间长的审核包优先排序。
- 页面初始加载同时返回真实数量，不允许先显示 0、点击后才更新。

### 8.3 审核项操作

- 右侧操作随 `review_type` 改变。
- 不再显示统一的“审核决定 + 处理方式”双重选择。
- `internal_action` 只在审核记录详情中以只读方式展示，默认折叠。
- 适用范围默认显示 AI 提取值，审核人可以修正；所有修改写入决策快照。
- 不提供事实正文编辑器。
- “退回修改”必须填写修改要求，并明确显示“资料需在飞书原文中修改”。
- “等待业务确认”必须填写待确认问题和责任人说明。

### 8.4 跨文档证据

- 默认显示关系摘要和关联文件名。
- 点击后展开相同内容、差异内容、适用范围和判断理由。
- 冲突内容保持明显高亮。
- 提交一个冲突审核项时，只关闭该项 `relation_ids`，不能关闭这份文档的全部关系。
- “跨文档检查”独立标签继续作为全局问题中心，不与审核工作区重复建设。

### 8.5 加工异常

以下内容不出现在审核包队列：

- 下载失败。
- 飞书权限不足。
- 解析、OCR 失败。
- 非目录节点但没有正文。
- 尚未完成正文质量检查。

它们继续出现在“资料与扫描 → 素材队列”，提供错误原因、查看详情和重试操作。重试成功并完成内容检查后，才允许生成审核包。

## 9. 数据迁移方案

### 9.1 迁移原则

- 不删除或覆盖现有审核记录。
- 不重新发布已有知识。
- 不因为迁移改变 `active_version_id`。
- 迁移脚本必须幂等，可重复执行。
- 在迁移完成并核对数量前不切换前端。

### 9.2 回填规则

每条现有 `feishu_governance_reviews` 回填为一个审核包和一个审核项：

| 旧状态 | 新审核包状态 | 新审核项状态 |
| --- | --- | --- |
| `pending` | `OPEN` | `PENDING` |
| `changes_requested` | `WAITING_SOURCE_CHANGE` | `WAITING_SOURCE_CHANGE` |
| `resolved` | `COMPLETED` | `DECIDED` |
| `rejected` | `COMPLETED` | `DECIDED` |

其他映射：

- 旧 `decision` 映射到业务 `outcome`，无法无歧义映射时保留原值并标记 `legacy_migrated=true`。
- 旧 `action` 原样保存到 `internal_action`。
- 旧 `problem_tags`、意见、适用范围、处理人和时间完整保留。
- 旧 `version_id` 作为 `subject_id`，`subject_type=MATERIAL_VERSION`。
- 旧版本关联的跨文档关系放入 `relation_ids`，同时标记 `legacy_relation_scope=DOCUMENT`。
- 旧 `changes_requested` 自动建立第一轮资料修改任务；责任人未知时为空，不阻塞迁移。

### 9.3 发布步骤

1. 部署数据库迁移和新模型，新接口暂不供前端使用。
2. 执行回填并核对审核包、审核项、状态数量。
3. 新扫描任务开始写入新审核模型；旧写接口进入兼容模式。
4. 切换 `FeishuReviewWorkspace` 使用新接口。
5. 观察一个发布周期，确认无数据遗漏后再将旧写接口改为只读。

发生问题时可以切回旧页面，因为旧表和旧记录未被删除。

## 10. 与现有发布链路的兼容

P0 不重写素材发布：

- `PUBLISH` 和 `ADOPT_NEW_VERSION` 仍调用现有 `FeishuReviewService.approve()` 和发布队列。
- 新版本发布成功后才切换当前有效版本。
- `KEEP_CURRENT`、`EXCLUDE`、`REQUEST_SOURCE_CHANGE` 不触发发布。
- 未解决的冲突审核项继续阻止发布。
- 但阻止范围改为当前审核项关联的关系，不再以“整份文档所有关系一起解决”为前提。
- 发布任务失败时，审核裁决保留，素材进入发布失败状态并允许重试，不要求重新审核。

## 11. 并发、幂等和权限

- 所有审核写操作校验当前负责人。
- 转交后原负责人提交返回 HTTP 403。
- 草稿、提交和转交使用 `lock_version` 防止覆盖。
- 提交接口接受请求幂等键；重复请求不得重复发布、重复写事件或重复创建修改任务。
- 审核包和审核项创建依赖唯一业务键，扫描或比较任务重试不产生重复数据。
- 只有管理员和超级管理员能够访问审核接口。
- 飞书链接、正文和引用仍遵循现有数据源访问权限，不在事件日志中写入凭据。

## 12. 测试与验收

### 12.1 后端测试

必须覆盖：

1. 同一来源版本重复处理只产生一个审核包。
2. 一个审核包可以包含多个审核项，包状态由审核项状态正确聚合。
3. 页面提交业务结果后，内部动作映射正确。
4. 不允许提交不属于当前审核类型的结果。
5. “退回修改”事务内创建资料修改任务。
6. 扫描到内容 Hash 变化的新版本后，旧审核项结束并创建新审核项。
7. 仅更新时间变化不重开审核。
8. 转交后旧负责人不能继续裁决，新负责人可以处理。
9. 保存草稿后刷新页面仍能读取。
10. 乐观锁冲突不会覆盖其他人的草稿或决定。
11. 一个冲突审核项的裁决只解决其关联关系。
12. 目录节点、解析失败和正文缺失不会创建审核包。
13. 新版本审核期间旧正式版本继续可用。
14. 重复提交不会重复发布或重复创建修改任务。
15. 发布失败后可以重试发布，不需要重新审核。
16. 迁移脚本重复运行不会重复回填。

### 12.2 前端测试

必须覆盖：

1. 初次进入页面立即显示真实待处理数量。
2. 审核包列表展示各类型审核项数量。
3. 不同审核类型显示不同业务按钮。
4. 页面不再出现通用内部动作下拉框。
5. 退回修改时未填写修改要求不能提交。
6. 草稿保存后刷新页面内容仍存在。
7. 转交后审核包从原负责人的“待我处理”中消失。
8. 冲突证据能够显示关联文件、相同内容和差异内容。
9. 加工异常不出现在审核包筛选中。
10. 长审核包支持审核项导航，提交操作不会意外滚动或丢失当前位置。

### 12.3 P0 完成标准

同时满足以下条件才算 P0 完成：

- 审核人员不再选择 `decision + action` 两套字段。
- 所有人工任务按审核包呈现，并可逐审核项裁决。
- “需要修改”能够跟踪到新飞书版本并重新审核。
- 转交、草稿、审核历史都真实持久化。
- 加工异常与知识问题彻底分流。
- 旧审核记录和已有正式知识没有丢失或重复发布。
- 现有飞书扫描、Yuxi 发布和知识助手检索保持可用。

## 13. 推荐开发拆分

按以下顺序开发，每一步都可独立验收：

1. **数据库与迁移**：新增三张表、状态枚举、约束、回填脚本及迁移测试。
2. **领域服务**：审核包聚合、场景化结果映射、草稿、转交和状态计算。
3. **资料修改闭环**：创建修改任务、新版本检测、重开审核。
4. **新 API**：列表、详情、草稿、提交、转交及兼容层。
5. **前端工作区**：队列改成审核包，右侧改成场景化操作。
6. **异常分流**：确保加工失败和正文缺失只留在素材队列。
7. **回归验收**：扫描、比较、审核、发布、旧版本可用性和权限测试。

P0 验收通过后，再进入 P1 的来源片段、逻辑知识、多来源关系和 RAG 去重。
