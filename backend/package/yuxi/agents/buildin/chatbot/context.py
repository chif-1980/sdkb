from dataclasses import dataclass, field

from yuxi.agents.context import BaseContext


@dataclass(kw_only=True)
class ChatBotContext(BaseContext):
    filesystem_read_only: bool = field(
        default=False,
        metadata={
            "hide": True,
            "configurable": False,
            "description": "仅允许方案 Agent 读取会话文件，禁止通过文件工具写入或执行命令。",
        },
    )
    subagents: list[str] | None = field(
        default=None,
        metadata={
            "name": "子智能体",
            "options": [],
            "description": "可选子智能体列表，为空表示启用当前用户可见的全部子智能体。",
            "type": "list",
            "kind": "subagents",
        },
    )
