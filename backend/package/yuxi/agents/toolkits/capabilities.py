"""Tools for consulting the governed enterprise capability map."""

from __future__ import annotations

from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool


class CapabilityCatalogInput(BaseModel):
    query: str = Field(default="", description="要匹配的客户需求或能力关键词")
    limit: int = Field(default=20, ge=1, le=50, description="最多返回的能力条目数")


@tool(
    category="buildin",
    tags=["企业能力"],
    display_name="匹配企业能力",
    args_schema=CapabilityCatalogInput,
)
async def match_enterprise_capabilities(
    query: str = "",
    limit: int = 20,
    runtime: ToolRuntime = None,
) -> list[dict] | str:
    """从当前用户可见的企业能力目录匹配能力，不创造目录之外的能力。"""
    runtime_context = getattr(runtime, "context", None)
    tenant_key = getattr(runtime_context, "tenant_key", None)
    try:
        from yuxi.product_chat.capability_repository import CapabilityCatalogRepository
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as db:
            capabilities = await CapabilityCatalogRepository(db).list_visible(
                query=query,
                tenant_key=tenant_key,
                limit=limit,
            )
    except Exception:
        return "企业能力目录暂不可用；请将能力覆盖标记为 UNKNOWN，不要自行声称已具备。"
    if not capabilities:
        return "未匹配到已登记的企业能力；请将相关需求标记为 UNKNOWN，并放入待确认或研发建议。"
    return capabilities
