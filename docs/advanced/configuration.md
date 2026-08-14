# 配置系统详解

## 概述

系统采用多层配置架构，模型配置由网页界面管理，应用配置基于 Pydantic + TOML。

## 配置层级

```
代码默认值 → TOML 文件 → 环境变量
   (低)                      (高)
```

## 模型配置

由网页统一管理，详见 [模型配置](../intro/model-config.md)。

## 应用配置

配置项定义于 `backend/package/yuxi/config/app.py`，用户修改保存至 `saves/config/base.toml`。

### 修改配置

```python
from yuxi.config import config

config.default_model = "provider-id:model-id"
config.save()
```

配置会在保存 `base.toml` 后写入 Redis 快照（`yuxi:runtime_config`）。快照包含可运行时同步的公开配置字段，不包含 `_` 开头的内部属性和 `save_dir`；API/worker 进程在启动时各拉起一个后台同步线程，按 5 秒间隔从该快照刷新内存值，读取端无需感知。Redis 不可用时继续使用当前内存值。

`save_dir` 是启动期内部路径配置，不在管理员配置中展示，也不支持通过管理员配置接口、`base.toml` 或运行时 Redis 快照修改。sandbox 相关配置仍属于启动期敏感配置，运行中的已初始化组件不承诺完整热更新，修改后需要重启服务保证生效。

如果 `base.toml` 损坏，删除 `saves/config/base.toml` 后重启服务即可回到代码默认配置。

## 飞书只读同步凭据

API 和 worker 从同一份本机环境文件读取固定的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。管理员不在 UI 中输入凭据，而是通过“检查连接”验证应用凭据及根节点读取权限。

后端自动向飞书 tenant token 认证端点发送 POST 请求，以换取 tenant token。token 只缓存在 API 或 worker 各自的进程内存中，并在过期前自动刷新；不会写入数据库、Redis 或文件。该认证请求是飞书调用中唯一的非 GET 请求，只用于换取访问凭据，不修改企业内容；其余 Wiki、Docx 和 Drive 访问均为只读 GET 请求。

数据源 API 和数据库业务配置不保存或公开凭据变量名、App ID、App Secret 或 tenant token 正文。历史兼容列仅写入固定内部标记，不承载凭据。真实凭据只保存在本机环境文件中，不得写入 TOML、日志、事件记录或版本库。
