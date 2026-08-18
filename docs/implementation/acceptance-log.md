# 实施验收记录

## 基线

- 日期：2026-08-12
- Yuxi：v0.7.1 / dfb3aa203ab3d6390465d99f718e5d7fce50eecb
- Compose project：quickdone-kb-yuxi
- 凭据：仅记录配置状态，不记录值

## 阶段结论

| 阶段 | 结果 | 证据摘要 | 操作者 |
| --- | --- | --- | --- |
| 1 | PASS | SiliconFlow BAAI/bge-m3 实时返回 1024 维有效向量；TXT、PDF、中文 PNG 均完成解析和自动入库；Milvus 检索命中 3 个来源，重启 API/worker 后数据仍可检索 | Codex + 用户 |
| 2 | RUNNING | 已验证企业自建应用认证、单素材受控扫描与发布，以及 PostgreSQL/MinIO/Milvus 持久化；权限审计、多类型全量目录和 Redis 状态恢复待验证 | Codex + 用户 |

## 阶段一当前证据

- 测试材料标记：`QD-PHASE1-EVIDENCE-20260813`
- 知识库：`阶段一能力验收库`（`kb_wkq6l7ai4w`）
- 服务状态：API healthy；worker running；PostgreSQL、Redis、Milvus、MinIO、etcd 保持运行
- 文件链路：PNG 上传成功，RapidOCR 中文识别成功，解析 Markdown 已写入 MinIO
- 向量链路：Milvus 集合已创建，维度为 1024；充值后 `/v1/embeddings` 实测返回 2 条有效向量（均为 1024 维、finite、nonzero）
- 入库结果：`phase1-evidence.txt`、`phase1-evidence.pdf`、`phase1-evidence.png` 均显示“已入库”，统计为 3 个 Chunks、297 Tokens
- 检索结果：问题“阶段一证据标记是什么，它来自哪些文件？”命中 3 个文档块，包含 `QD-PHASE1-EVIDENCE-20260813`，来源为 TXT、PDF、PNG；PDF 片段保留“第 1 页/共 2 页”和“第 2 页/共 2 页”
- 持久化结果：仅重启 API/worker 后，3 个文件与 3 个 Chunks 仍存在，重复检索仍命中三类来源
- 当前结论：阶段一验收通过

## 阶段二当前证据

- 离线端到端测试：4 个用例覆盖三层知识树完整扫描、PDF/TXT/PNG 的发现、归档及 parse/index 编排、音频/视频发现并标记暂不支持、审核、发布和飞书引用；增量 v2 替换及发布前 v1 持续生效；权限与局部遍历失败、429 重试、不支持媒体和日志令牌脱敏；内存 Milvus 适配器强制失败时保留 v1，并将 v2 标记为 `publish_failed`。该测试未运行真实 PDF/OCR 解析器，也未连接真实 MinIO/Milvus
- 执行命令：`uv run --project backend --no-sync --no-dev pytest -q backend/test/e2e/test_feishu_knowledge_pipeline_e2e.py`
- 执行结果：`4 passed`；另有 2 条既有弃用警告
- 离线认证单元测试：61 个用例覆盖 tenant token 换取与内存缓存、到期刷新、401 后刷新并重放一次、并发首次请求只换取一次 token，以及日志和异常信息脱敏
- 执行命令：`uv run --project backend --no-sync --no-dev pytest -q backend/test/unit/integrations/test_feishu_client.py`
- 执行结果：`61 passed`；另有 1 条既有弃用警告
- 真实认证与受控扫描：API/worker 使用同一份本机企业自建应用配置换取 tenant token；检查连接成功后执行限定根目录扫描，数据库保留 2 个扫描批次（1 个成功、1 个失败诊断批次），成功批次扫描 1 个素材并完成归档、解析、审核和发布
- 重启持久化：仅重启源码 API/worker 后，PostgreSQL 仍保留 1 个数据源、1 个成功批次、1 个素材版本；该版本为 `published`/`approved`，关联 Yuxi 文件并有 1 个 Chunk；MinIO 原始对象存在且非空，Milvus 检索仍可命中
- 引用回填：检索结果会从 `knowledge_files.processing_params.feishu` 回填 `source_url`、`wiki_path`、`material_version`、`page_info`，普通文件不会被错误添加飞书引用；回归测试覆盖字段白名单
- 安全检查：真实凭据未写入仓库、测试材料或日志；仓库扫描仅发现空配置模板、假值测试和变量名说明
- 权限审计：通过飞书官方只读接口核对到企业应用当前有 31 项已授权 scope，其中包含事件订阅、权限成员、搜索、查看记录、表格、幻灯片和思维笔记等当前流水线不会调用的权限；当前不满足最小权限原则
- 目录权限探测：指定根节点认证、Wiki 节点读取、子节点列表、Docx 正文和块读取均成功；该根节点当前只有 1 个 Docx 节点、0 个子节点且未发现附件，因此目录递归范围和附件下载权限仍无法验收
- 待完成：在飞书开放平台回收当前流水线不使用的授权，只保留 Wiki 节点读取、Docx 内容读取和附件下载所需的只读权限；同时使用包含子节点和附件的真实目录复验资源边界
- 待完成：对包含产品手册、解决方案、部署文档、会议纪要等真实多类型素材的完整目录执行扫描、加工、审核、发布与引用验收；音视频加工按当前决策置于最低优先级
- 待完成：重启 Redis、API 和 worker 后，验证扫描任务、审核状态及失败任务诊断信息能够正确恢复
- 当前结论：阶段二仍为 `RUNNING`，完成上述验收后才能标记为 `PASS`

## 企业知识助手阶段 1 验收（2026-08-16）

本节只记录独立 React 问答入口和 FastAPI 产品问答链路，不改变上文知识加工阶段的结论，也不包含阶段 2 的个人资料上传、附件、临时索引或音视频处理。

| 验收项 | 状态 | 脱敏证据 |
| --- | --- | --- |
| 飞书登录与产品会话 | PASS | 使用已发布的企业自建应用完成真实 OAuth 登录；登录后直接进入 `/chat`，产品会话 Cookie 可访问 `/api/session` |
| 正式知识回答 | PASS | 问题“部门文档目录可以存放哪些资料？”返回 `SUPPORTED`，生成 1 条企业引用；引用详情可读取，打开接口返回 307 并指向 HTTPS 飞书页面；引用 ID 记为 `01M04WYP…KJSHB` |
| 依据不足回答 | PASS | 问题“创新应用事业部2027年员工食堂菜单是什么？”返回 `INSUFFICIENT`，回答精确为“暂无足够可靠资料”，引用数为 0 |
| 冲突回答 | PASS | 问题“商机会议纪要应该如何命名？”返回 `CONFLICTING`，生成 2 条企业引用；回答状态和引用已持久化 |
| 会话持久化 | PASS | 本次真实三态验收创建的会话均在验收结束后归档；最近三种助手消息分别保留 1、0、2 条引用 |
| 独立问答界面 | PASS | 在 1440×900、1024×768、390×844 三个真实视口验证：无横向溢出，不显示 Yuxi、模型、Agent、Skill、知识库或 `@` 技术入口，消息区与输入区边界正确 |
| 固定输入区与移动抽屉 | PASS | 390×844 下强制长内容后消息区可独立滚动，输入区位置保持不变且始终可见；来源抽屉覆盖手机视口，背景为 inert，并提供“打开飞书原文”入口 |
| 浏览器运行错误 | PASS | 三个视口及来源抽屉验收期间新增控制台错误 0 个、页面异常 0 个；脱敏截图保存在 gitignored 的 `artifacts/acceptance/enterprise-assistant-phase-1/` |
| 无权限真实用户 | BLOCKED | 当前只有 1 个已绑定真实飞书用户，无法完成第二个真实无权限成员的召回拒绝和引用打开拒绝验收；自动化测试已覆盖默认拒绝，但不能代替真实账号验收 |

### 本次真实知识链路

- 增量扫描批次：`9fc5875c6ac54466bc291a44ccd35832`，结果为 `scanned=2 / new=1 / unchanged=1 / failed=0`。
- 冲突验收资料：`【验收测试】商机会议纪要命名规则冲突`，素材版本 `d14a7adb536745a58d6de015280facc5`，当前为 `published / approved`，包含 1 个 Chunk。
- 真实产品 E2E：`test/e2e/product_chat/test_enterprise_answer_flow.py`，结果为 `1 passed`，同时覆盖 `SUPPORTED / INSUFFICIENT / CONFLICTING` 和正式引用打开。
- 安全边界：测试会话令牌只在本机进程内生成和使用，未写入测试文件、截图、命令输出或本记录；本节未复制企业文档正文。

### 当前结论与待决事项

- 除真实无权限用户场景外，企业知识助手阶段 1 的登录、三态问答、引用、持久化和三视口界面验收均已通过。
- 企业知识助手阶段 1 暂保持 `RUNNING`，不能在缺少真实无权限账号证据时标记为完整 `PASS`。
- 冲突验收资料当前仍处于正式索引，会影响相同问题的正式回答；验收完成后是保留、迁出还是作废，需由业务方明确决定，当前不擅自删除或下线。

## 企业知识助手阶段 1 下架补充验收（2026-08-17）

- 业务处置：用户选择方案 1，将冲突验收资料迁出配置的飞书 Wiki 根目录，再通过正式知识加工流程识别来源失效并确认下架；未直接修改数据库或绕过管理页面。
- 扫描结果：真实全量扫描批次 `52a8fee9bf4e4a0fabc74a5fbc5ac5da` 执行成功，结果为 `scanned=1 / unchanged=1 / failed=0 / invalidated=1`；冲突素材显示为“来源失效”。
- 下架结果：素材版本 `d14a7adb536745a58d6de015280facc5` 通过管理页面确认下架后为 `removed / approved`，活动版本指针和 Yuxi 文件关联均已清空；追加式事件记录包含 `removal_started` 和 `removal_confirmed`。
- 清理结果：原关联 Yuxi 文件在 `knowledge_files` 中为 0 条，关联 Chunk 在 `knowledge_chunks` 中为 0 条，Milvus 集合中的关联向量为 0 条。
- 问答复验：重新提问“商机会议纪要应该如何命名？”后返回 `SUPPORTED`，仅生成 1 条正式企业引用；引用来自仍有效的正式目录根文档，不再包含已下架的冲突素材；验收会话已归档。
- 回归检查：飞书扫描服务单元测试 `22 passed`，Ruff 检查通过；新增覆盖迁到云盘、移出配置根目录、父节点无权限和父级链循环四种边界。
- 阶段结论：企业知识助手阶段 1 仍为 `RUNNING`；仅真实无权限用户验收继续 `BLOCKED`，原因是当前没有第二个真实飞书账号，不使用伪造账号替代。

## 知识加工管理员扫码授权（2026-08-17）

- 实现结果：知识加工页以“扫码授权”为主入口，在本地生成飞书新版 OAuth 链接二维码；保留原浏览器跳转授权作为备用入口。二维码不包含应用密钥、access token 或 refresh token。
- 权限范围：扫码与浏览器授权均申请 `offline_access` 和 `wiki:wiki:readonly`，未采用不支持 scope 的飞书旧版二维码 SDK。
- 回调配置：飞书开放平台已添加手机可访问的局域网扫码回调，页面显示“当前修改均已发布”；本机 API 已读取 `FEISHU_KNOWLEDGE_QR_REDIRECT_URI`。
- 自动识别：电脑端每 2 秒轮询授权状态，只在 `last_refreshed_at` 晚于本次扫码发起时间时判定成功，随后自动刷新授权身份与知识目录。
- 自动化验证：相关后端、集成与前端回归为 `292 passed / 8 skipped` 和 `23 passed`；Ruff、ESLint、前端生产构建及 `git diff --check` 通过。
- 部署验证：API 为 healthy，worker 与管理端运行；`127.0.0.1:5173` 和 `172.16.26.50:5173` 均返回 200；手机回调失败页能够在不暴露授权码或令牌的情况下返回稳定错误提示。
- 待验收：服务重建后管理端登录会话失效，尚未执行管理员本人手机扫码的最终真实验收；重新登录后仅需点击“扫码授权”并在手机飞书确认。此过程未触发全量或增量扫描。
