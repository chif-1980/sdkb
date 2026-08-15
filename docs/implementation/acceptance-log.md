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
- 待完成：审计企业自建应用权限范围，确认应用只具备指定知识目录所需的最小权限
- 待完成：对包含产品手册、解决方案、部署文档、会议纪要等真实多类型素材的完整目录执行扫描、加工、审核、发布与引用验收；音视频加工按当前决策置于最低优先级
- 待完成：重启 Redis、API 和 worker 后，验证扫描任务、审核状态及失败任务诊断信息能够正确恢复
- 当前结论：阶段二仍为 `RUNNING`，完成上述验收后才能标记为 `PASS`
