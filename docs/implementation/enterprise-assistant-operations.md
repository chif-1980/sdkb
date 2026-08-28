# 企业知识助手阶段 1 运维说明

本文只覆盖阶段 1 的独立知识问答链路。正式凭据只保存在部署机器的本地 `.env` 或密钥管理系统中，不写入仓库、命令参数、日志或验收记录。

## 1. 准备配置

在 Yuxi 后端仓库根目录基于 `.env.example` 创建本地 `.env`，并在本机填写以下配置：

- `FEISHU_APP_ID`：飞书企业自建应用 App ID。
- `FEISHU_APP_SECRET`：飞书企业自建应用 App Secret。
- `FEISHU_PRODUCT_REDIRECT_URI`：飞书登录完成后的产品回调地址。
- `FEISHU_KNOWLEDGE_REDIRECT_URI`：知识加工管理员完成飞书用户授权后的回调地址。
- `FEISHU_KNOWLEDGE_QR_REDIRECT_URI`：管理员使用手机飞书扫码授权后的回调地址；该地址必须能从手机访问。
- `FEISHU_OAUTH_TOKEN_ENCRYPTION_KEY`：可选的 Fernet 密钥；为空时使用 `JWT_SECRET_KEY` 经域隔离派生。
- `PRODUCT_FEISHU_SOURCE_ID`：企业知识助手唯一允许检索的、处于启用状态的飞书知识源 ID。
- `YUXI_CORS_ORIGINS`：前后端跨域部署时允许访问 API 的前端来源；同源部署可留空。

飞书企业自建应用还必须开通以下通讯录权限，并将权限范围覆盖需要使用知识助手的员工及其部门：

- `contact:user.base:readonly`：读取员工基本信息和在职状态；
- `contact:department.base:readonly`：读取部门基础信息。

登录服务使用企业 `tenant_access_token` 查询通讯录，因此其他企业、已停用、已离职或不在应用通讯录可见范围内的账号不会被自动开户。权限缺失时登录页会提示管理员检查通讯录权限。

本地开发时，产品回调地址应使用用户实际访问的 React 地址：

```text
http://127.0.0.1:5174/api/auth/feishu/callback
```

生产环境应替换为产品站点的 HTTPS 同源地址。不要把后端内部地址、容器名或 `localhost:5050` 注册成面向用户的生产回调地址。

## 2. 启动数据与检索服务

在 Yuxi 后端仓库根目录执行：

```bash
docker compose --project-name quickdone-kb-yuxi \
  -f compose.phase1.yml --env-file .env \
  up -d postgres redis minio etcd milvus sandbox-provisioner
```

等待 `postgres`、`redis`、`minio`、`etcd`、`milvus` 和 `sandbox-provisioner` 进入健康状态后再启动应用服务：

```bash
docker compose --project-name quickdone-kb-yuxi \
  -f compose.phase1.yml --env-file .env \
  up -d --build api worker
```

## 3. 启动独立问答前端

在 React 企业知识助手仓库执行：

```bash
npm ci
npm run dev:web -- --host 127.0.0.1 --port 5174
```

浏览器访问 `http://127.0.0.1:5174/login`。开发服务器会把 `/api` 请求代理到 `http://127.0.0.1:5050`，普通用户登录后只进入“企业知识助手”。`5173` 保留给现有 Yuxi 管理端，避免两个前端互相占用端口。

## 4. 配置飞书回调

在飞书开放平台的企业自建应用中登记与 `FEISHU_PRODUCT_REDIRECT_URI` 完全一致的重定向 URL，包括协议、域名、端口和路径。修改后重启 `api`，再从 `/login` 发起登录；不要直接手工访问回调地址。

知识加工管理页支持两种授权入口，两者都使用飞书新版 OAuth 并申请 `offline_access` 与 `wiki:wiki:readonly`：

- 浏览器授权使用 `FEISHU_KNOWLEDGE_REDIRECT_URI`，本地可配置为 `http://127.0.0.1:5173/api/feishu-knowledge/oauth/callback`。
- 扫码授权使用 `FEISHU_KNOWLEDGE_QR_REDIRECT_URI`。本地开发应替换为电脑当前的局域网地址，例如 `http://172.16.26.50:5173/api/feishu-knowledge/oauth/callback?flow=qr`；手机与电脑需处于同一可互访网络。

两个地址都要逐字添加到飞书应用的“安全设置 / 重定向 URL”中。局域网 IP 变化后，需要同时修改本机 `.env` 与飞书开放平台中的扫码回调地址，并重启 `api`。生产环境应改用手机可访问的 HTTPS 同源域名。

飞书官方旧版二维码登录 SDK 不支持携带上述 scope，因此知识加工页由浏览器在本地生成新版 OAuth 链接的二维码，不调用第三方二维码服务，也不把 access token、refresh token 或应用密钥放进二维码。

## 5. 绑定正式知识源

`PRODUCT_FEISHU_SOURCE_ID` 填写系统中飞书知识源记录的 ID，不是飞书 Wiki 链接中的节点 token。该知识源必须满足：

- 状态为启用；
- 指向已确认的企业知识根目录；
- 需要回答的素材版本已经审核、发布并完成向量索引。

企业知识助手不会从其他知识源回退检索。变量缺失、记录不存在或知识源停用时，接口应明确返回知识源不可用。

## 6. 自动开户与组织同步

无需在 Yuxi 管理后台预先创建普通员工。员工首次完成飞书登录时，系统会使用企业通讯录确认账号与在职状态，然后自动：

- 创建角色固定为 `user` 的内部账号，并以飞书 `user_id` 作为稳定 UID；
- 建立 `open_id`、`user_id`、`union_id` 和企业租户绑定；
- 创建或复用对应的本地部门，并保存员工的全部飞书部门成员关系；
- 后续每次登录刷新姓名、头像、在职状态及部门关系。

已有预绑定账号会继续复用，不会重复创建。已软删除用户、已撤销绑定、企业不一致、停用或离职员工仍按默认拒绝处理；管理员和超级管理员角色不会通过飞书首次登录自动授予。

## 7. 健康与功能检查

先检查服务状态和 API：

```bash
docker compose --project-name quickdone-kb-yuxi \
  -f compose.phase1.yml --env-file .env ps
curl --fail --silent http://127.0.0.1:5050/api/system/healthz
curl --fail --silent http://127.0.0.1:5174/login >/dev/null
```

再用预绑定飞书用户完成以下验收：

1. 从 `/login` 登录后直接进入 `/chat`。
2. 正式知识问题显示“有正式资料支持”，引用详情可打开飞书原文。
3. 未收录问题精确回答“暂无足够可靠资料”。
4. 冲突资料问题显示“资料存在冲突”。
5. 新建、切换、归档对话正常，手机端输入框始终可见。

真实自动化验收仅在本机安全设置以下变量后运行：

- `PRODUCT_E2E_SESSION_COOKIE`
- `PRODUCT_E2E_SUPPORTED_QUESTION`
- `PRODUCT_E2E_INSUFFICIENT_QUESTION`
- `PRODUCT_E2E_CONFLICTING_QUESTION`

```bash
cd backend
RUN_REAL_PRODUCT_E2E=1 uv run pytest \
  test/e2e/product_chat/test_enterprise_answer_flow.py -m e2e -v
```

不要把会话 Cookie 写入测试文件、截图、命令参数或验收记录。

## 8. 回滚

回滚前分别记录后端和 React 前端当前提交。切换到上一个已经通过验收的提交后，重新构建后端 `api`、`worker`，并重新构建或启动 React 前端；PostgreSQL、MinIO 和 Milvus 数据卷保持不动。

```bash
git switch --detach <上一个已验证提交>
docker compose --project-name quickdone-kb-yuxi \
  -f compose.phase1.yml --env-file .env \
  up -d --build api worker
```

回滚后重新执行健康检查、飞书登录、正式知识问答和引用打开验收。不要删除数据卷，也不要用工作区重置命令覆盖未提交的配置或验收记录。
