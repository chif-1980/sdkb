# 生产部署指南

本文档介绍如何在生产环境中部署 ZhiShu。

## 前置要求

- Docker Engine (v24.0+)
- Docker Compose (v2.20+)
- NVIDIA Container Toolkit（如需使用 GPU 服务）

阶段一隔离部署要求 Docker Compose (v2.24.4+)，因为 `compose.phase1.yml` 使用 `!override` 清除上游开发态挂载。

::: warning 注意事项
1. 生产环境和开发环境建议使用不同的机器，避免端口和资源冲突
2. 虽然名为「生产环境」，但这只是基本配置，真正上线需要根据实际情况调整
3. 前端有调试面板（长按侧边栏触发），生产环境建议关闭
:::

## 部署步骤

### 1. 准备配置文件

为避免与开发环境冲突，生产环境建议使用 `.env.prod` 文件：

```bash
cp .env.template .env.prod
```

编辑 `.env.prod`，设置强密码和必要的 API 密钥：

```sh
POSTGRES_PASSWORD=
NEO4J_PASSWORD=
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
JWT_SECRET_KEY=
YUXI_INSTANCE_ID=
SANDBOX_PROVISIONER_TOKEN=
SILICONFLOW_API_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
```

生产 Compose 会在前七项配置缺失或为空时拒绝启动，并提示具体变量名。`JWT_SECRET_KEY` 和 `SANDBOX_PROVISIONER_TOKEN` 均应至少使用 32 字节随机值并持久保存，可分别使用 `openssl rand -hex 32` 生成；两者不能复用。`YUXI_INSTANCE_ID` 应是每套部署稳定且唯一的实例标识。模型 API 密钥按实际使用的供应商配置。

部署前，API 和 worker 必须通过同一份本机环境文件同时读取 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。开发环境使用本机 `.env`，生产环境使用本机 `.env.prod`；这些文件及其中的凭据不得提交到版本库，也不得输出到日志。企业知识库正文或附件内容不得复制到测试、文档或提交记录中。

可用以下命令只检查 `.env.prod` 中两个变量是否为非空配置，不输出真实值；该检查不验证凭据是否有效：

```sh
if [ ! -f .env.prod ]; then
  printf '未找到 .env.prod\n'
  exit 1
fi
awk '
function trim(value) {
  gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
  return value
}
function strip_inline_comment(value, position, character, quote) {
  quote = ""
  for (position = 1; position <= length(value); position++) {
    character = substr(value, position, 1)
    if ((character == "\"" || character == "\047") && (quote == "" || quote == character)) {
      if (quote == "") {
        quote = character
      } else {
        quote = ""
      }
    } else if (character == "#" && quote == "" && (position == 1 || substr(value, position - 1, 1) ~ /[[:space:]]/)) {
      return substr(value, 1, position - 1)
    }
  }
  return value
}
function configured(value, length_value, first, last) {
  value = strip_inline_comment(value)
  value = trim(value)
  length_value = length(value)
  if (length_value >= 2) {
    first = substr(value, 1, 1)
    last = substr(value, length_value, 1)
    if ((first == "\"" && last == "\"") || (first == "\047" && last == "\047")) {
      value = trim(substr(value, 2, length_value - 2))
    }
  }
  return length(value) > 0
}
BEGIN {
  keys[1] = "FEISHU_APP_ID"
  keys[2] = "FEISHU_APP_SECRET"
}
{
  for (position = 1; position <= 2; position++) {
    key = keys[position]
    pattern = "^[[:space:]]*" key "[[:space:]]*="
    if ($0 ~ pattern) {
      value = $0
      sub(pattern, "", value)
      values[key] = value
    }
  }
}
END {
  for (position = 1; position <= 2; position++) {
    key = keys[position]
    if (configured(values[key])) {
      printf "%s：已配置（未显示值）\n", key
    } else {
      printf "%s：未配置\n", key
    }
  }
}
' .env.prod
```

### 2. 启动服务

生产环境必须使用受信任的 HTTPS 证书。先在 `.env.prod` 中配置证书链和私钥在宿主机上的绝对路径：

```dotenv
YUXI_TLS_CERT_PATH=/etc/letsencrypt/live/your-domain/fullchain.pem
YUXI_TLS_KEY_PATH=/etc/letsencrypt/live/your-domain/privkey.pem
```

证书文件不得复制进仓库或镜像。使用生产配置和 TLS 覆盖配置启动：

```bash
# 仅启动核心服务（CPU 模式）
docker compose --env-file .env.prod \
  -f docker-compose.prod.yml -f docker-compose.tls.yml up -d --build

# 启动所有服务（包含 GPU OCR）
docker compose --env-file .env.prod \
  -f docker-compose.prod.yml -f docker-compose.tls.yml --profile all up -d --build
```

TLS 配置会监听 443，并把 80 端口的所有请求永久跳转到同主机的 HTTPS 地址。内网 IP 部署也必须使用终端设备信任的企业 CA 证书；不能用自签名证书代替生产传输保护。

### 3. 验证部署

- Web 访问：`https://your-domain`
- 公开最小健康检查：`curl https://your-domain/api/system/healthz`
- 登录后健康详情：`GET /api/system/health`（需要 Bearer Token）

公开头像和 Agent 图片通过前端同源路径 `/minio/public/...` 读取，由 Nginx 只读代理到 MinIO 的 `public` bucket。无需也不应向公网开放 MinIO 的 `9000` 对象 API 或 `9001` 管理控制台；知识库等私有 bucket 不经过这个代理。需要使用独立静态资源域名时，可在 `.env.prod` 中设置 `MINIO_PUBLIC_URL=https://assets.example.com`，并在该域名侧保持同等的只读 bucket 限制。

历史 PDF 解析 Markdown 中已经写入的 `http://localhost:9000/public/...` 或其他 `<host>:9000/public/...` 图片地址，会在前端渲染时自动转换为同源路径，不需要重新解析 PDF。

## 0.6.0 飞书统一登录与升级核对

善达知枢管理端与企业知识助手共用飞书应用和后端身份绑定。除飞书应用凭据外，部署时需核对以下配置：

| 配置 | 用途 |
| --- | --- |
| `FEISHU_PRODUCT_REDIRECT_URI` | 两端发起飞书登录时使用的共享后端回调，例如 `https://assit.quickdone.cn/api/auth/feishu/callback` |
| `ENTERPRISE_ASSISTANT_URL` | 管理端“企业知识助手”菜单的目标 origin；为空时从 `FEISHU_PRODUCT_REDIRECT_URI` 自动推导，例如 `https://assit.quickdone.cn` |
| `FEISHU_MANAGER_ORIGINS` | 允许登录后返回的知枢地址，多个地址用逗号分隔；仅填写协议、域名和端口，不带路径，例如 `https://manager.assit.quickdone.cn` |
| `FEISHU_KNOWLEDGE_REDIRECT_URI` | 知识访问授权的浏览器回调 |
| `FEISHU_KNOWLEDGE_QR_REDIRECT_URI` | 知识访问授权的手机扫码回调，须能从手机访问 |
| `FEISHU_OAUTH_TOKEN_ENCRYPTION_KEY` | 已保存知识访问凭据使用的加密密钥；未配置时沿用系统的 `JWT_SECRET_KEY` 派生方式，升级时保持原密钥不变 |

飞书开放平台需登记实际使用的 OAuth 回调地址。知枢登录结束后返回 `/auth/feishu/callback` 页面，管理域名应能访问该前端路由及同源 API。该前端页面与飞书开放平台登记的后端回调用途不同。

**身份与权限：** 首次飞书登录只创建普通用户，不自动授予管理角色。系统管理员先授权管理角色，用户再使用同一飞书身份登录知枢；可在“飞书账号与授权”查看绑定和角色。知识源会尝试复用同一身份已有的知识授权，并核验企业和目标资料访问权；登录成功不能代替知识访问授权。

管理端的“企业知识助手”菜单通过一次性、60 秒有效的登录交接码进入助手，不会把管理端令牌放进地址栏。交接成功后助手端写入独立的 HttpOnly 会话 Cookie；交接失败或过期时回到助手登录页。助手端仍会重新校验账号状态和部门归属。

管理端会在左侧菜单保持可见的右侧内容区内嵌助手页面。生产环境的助手站点必须允许 `https://manager.assit.quickdone.cn` 作为 CSP `frame-ancestors`，并移除会阻止跨域嵌入的 `X-Frame-Options: DENY`。仓库中的 `docker/nginx/nginx.conf` 用于管理端，已在 `frame-src` 中放行 `https://assit.quickdone.cn`，同时保留管理端自身禁止被嵌入的策略。使用其他域名时需同步更新目标地址及对应的 CSP 白名单。

**升级核对：**

1. 备份数据库和本地配置，配套更新 `sdkb` 与 `sdAssit` 0.6.0 的应用服务，保留现有数据卷和密钥。
2. 验证本地超级管理员仍可登录，再验证已授权管理角色的飞书账号登录。正常升级保留账号；清空数据库或改用新数据库不会自动保留原账号。
3. 在助手提交演示会议，核对后台状态与管理端“会议管理”的企业范围、纪要、待办及知识建议。
4. 对明确用于验收的事项执行确认发送，分别核对飞书通知和待办是否成功；改派后应更新原任务，日期应与界面一致。应用还需具备相应通讯录、消息及任务权限，并发布权限变更。
5. 验证历史会议引用、正文修订和 Word 导出；知识建议经维护人员处理后，仍通过既有知识审核发布流程生效。

配置模型请使用[模型供应商与统一模型配置](../intro/model-config)。GitHub 发布标签不代表生产服务已经完成上述更新和验收。

## 跨域（CORS）配置

`docker-compose.prod.yml` 默认把 `YUXI_ENV` 设为 `production`，后端在该环境下会按 `YUXI_CORS_ORIGINS` 显式声明允许的来源。**未配置时返回空列表，浏览器跨域请求会被拒绝**。生产部署前请根据前端与 API 的相对位置选择策略：

| 部署形态 | 推荐配置 |
|----------|----------|
| 前端与 API 同源（Nginx 同端口反代） | 不需要设置，留空即可 |
| 前端与 API 跨域部署 | `YUXI_CORS_ORIGINS=https://your-frontend.example.com` |
| 多个前端域名 | 逗号分隔，如 `https://a.example.com,https://b.example.com` |
| 完全放开（不推荐） | `YUXI_CORS_ORIGINS=*`，会自动关闭 credentials，登录态/JWT 无法跨域携带 |

开发环境（`YUXI_ENV=development` 且未设置该变量）默认允许 `http://localhost:5173` 与 `http://127.0.0.1:5173`，方便本地前后端独立启动调试。从 0.7.0 升级到 0.7.1 时，如果此前是跨域部署但未显式声明来源，必须补上 `YUXI_CORS_ORIGINS`，否则前端跨域请求会被拒绝。

## 维护与更新

### 从使用默认凭据的版本升级

如果部署曾使用仓库历史默认的 PostgreSQL、Neo4j 或 MinIO 凭据，升级 Compose 文件本身不会保证已有数据卷中的服务凭据已经改变。升级前应分别通过对应服务的管理命令真实修改凭据，再把新值写入 `.env.prod`；完成后重建相关服务，并使用旧凭据验证登录已被拒绝。

PostgreSQL 可以在数据库容器内使用交互式命令修改，避免新密码出现在 shell 历史和进程参数中：

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec postgres psql -U postgres -d yuxi -c '\password postgres'
```

Neo4j 应使用 `cypher-shell` 的当前用户密码修改流程；MinIO 应使用 `mc admin` 或部署所采用的密钥管理流程。不要把真实密码写入文档、测试脚本或命令历史。完成凭据轮换并配置 `SANDBOX_PROVISIONER_TOKEN` 后，再执行下面的重建命令。

### 更新代码

```bash
# 拉取最新代码
git pull

# 重新构建并启动
docker compose --env-file .env.prod \
  -f docker-compose.prod.yml -f docker-compose.tls.yml up -d --build
```

生产 Compose 不再向宿主机发布 PostgreSQL 和文档解析服务端口。确需从宿主机维护时，优先使用 `docker compose exec`；不要为了临时调试把这些端口重新暴露到公网。

### 查看日志

```bash
# API 日志
docker logs -f api-prod

# Nginx 访问日志
docker logs -f web-prod
```

## 飞书知识流水线验收与更新

飞书知识流水线必须按以下顺序受控验收：

1. 完成 API 健康检查。
2. 在管理界面执行“检查连接”，验证企业自建应用凭据及根节点读取权限。
3. 确认应用权限限定在目标知识空间、根节点和所需的只读范围。
4. 执行受控全量扫描，核对根节点标题、子节点数量，以及 PDF、TXT、PNG、音频、视频附件类型。
5. 核对日志未出现 App Secret 或 tenant token 正文，也未写入企业正文或附件内容。
6. 只重启 API 和 worker，随后确认 PostgreSQL、Redis、MinIO、Milvus 中的状态与检索结果保持不变。

完成上述步骤前，不得声称真实飞书认证或阶段二验收通过。

配置凭据或更新飞书流水线相关代码后，只重建运行本次源码的 API 和 worker，保留数据库与对象存储：

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build --no-deps api worker
```

禁止执行 `docker compose down -v`，也不得删除或重置 PostgreSQL、Redis、MinIO、Milvus 或 etcd 命名卷；不得停止无关的旧 Docker 服务。API 和 worker 重启后，应重新核对 PostgreSQL 中的来源、版本和审核状态，Redis 中的任务状态，MinIO 中的原始及处理后对象，以及 Milvus 检索结果和飞书引用。
