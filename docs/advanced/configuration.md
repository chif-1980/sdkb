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

飞书知识同步客户端仅从进程环境变量读取访问令牌，默认变量名为 `FEISHU_ACCESS_TOKEN`。数据源可以保存其他变量名，但只保存变量名，绝不保存令牌正文。

在部署环境中由管理员配置该变量；不要把令牌写入 `.env.example`、配置文件、日志、事件记录或提交。客户端只发送 GET 请求，不会写回飞书页面、云空间或多维表格。
