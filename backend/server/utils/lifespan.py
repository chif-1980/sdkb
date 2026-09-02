import os
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from yuxi.services.task_service import tasker
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
from yuxi.agents.mcp.service import ensure_builtin_mcp_servers_in_db
from yuxi.models.providers.service import ensure_builtin_model_providers_in_db
from yuxi.services.run_queue_service import close_queue_clients, get_redis_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.neo4j import close_shared_neo4j_connection
from yuxi.knowledge.runtime import knowledge_base
from yuxi.utils import logger
from yuxi.agents.backends.sandbox import init_sandbox_provider, shutdown_sandbox_provider
from yuxi import get_version
from yuxi.config import config
from yuxi.governance.retry_coordinator import retry_coordinator


async def _recover_recent_feishu_processing_tasks() -> int:
    """Requeue Feishu material tasks interrupted by the last API restart.

    Tasker persists task rows, but coroutine callbacks live in process memory.
    Recover only the recent restart marker so an API restart does not strand a
    just-submitted scan while leaving older, intentionally failed tasks alone.
    """
    from sqlalchemy import select

    from server.routers.feishu_knowledge_router import FeishuReviewService, _enqueue_processing
    from yuxi.storage.postgres.models_business import TaskRecord
    from yuxi.utils.datetime_utils import utc_now_naive

    # Task timestamps are stored as UTC-naive values while the UI displays
    # local time; keep a full day so a morning restart can recover a scan that
    # was submitted late the previous evening.
    cutoff = utc_now_naive() - timedelta(hours=24)
    async with pg_manager.get_async_session_context() as session:
        result = await session.execute(
            select(TaskRecord).where(
                TaskRecord.type == "feishu_process",
                TaskRecord.status == "failed",
                TaskRecord.message.in_({"服务重启时任务中断", "服务重启时任务未继续执行"}),
                TaskRecord.created_at >= cutoff,
            )
        )
        interrupted_tasks = list(result.scalars())

    recovered = 0
    for task in interrupted_tasks:
        version_id = (task.payload or {}).get("version_id")
        if not version_id:
            continue
        try:
            async with pg_manager.get_async_session_context() as session:
                await FeishuReviewService(session).retry(
                    version_id,
                    operator_id="system-recovery",
                )
                await session.commit()
            await _enqueue_processing(version_id, operator_id="system-recovery")
            recovered += 1
        except (LookupError, ValueError) as exc:
            logger.warning("Skipping Feishu task recovery for {}: {}", version_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to recover Feishu task {}: {}", version_id, exc)
    return recovered


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan事件管理器"""
    # 初始化数据库连接
    try:
        pg_manager.initialize()
        await pg_manager.create_tables()
        await pg_manager.ensure_product_schema()
        await pg_manager.ensure_business_schema()
        await pg_manager.ensure_knowledge_schema()
    except Exception as e:
        logger.error(f"Failed to initialize database during startup: {e}")

    # 确保内置 MCP 服务器定义存在于数据库
    try:
        await ensure_builtin_mcp_servers_in_db()
    except Exception as e:
        logger.error(f"Failed to ensure builtin MCP servers during startup: {e}")

    try:
        from yuxi.agents.skills.service import init_builtin_skills

        async with pg_manager.get_async_session_context() as session:
            await init_builtin_skills(session)
    except Exception as e:
        logger.error(f"Failed to initialize builtin skills during startup: {e}")

    try:
        from yuxi.repositories.agent_repository import AgentRepository

        async with pg_manager.get_async_session_context() as session:
            repository = AgentRepository(session)
            await repository.ensure_default_agent()
            await repository.ensure_general_purpose_subagent()
            await repository.ensure_web_search_subagent()
            await repository.ensure_deep_research_agents()
    except Exception as e:
        logger.error(f"Failed to ensure default agent during startup: {e}")

    # 初始化内置模型供应商配置
    try:
        async with pg_manager.get_async_session_context() as session:
            await ensure_builtin_model_providers_in_db(session)
    except Exception as e:
        logger.error(f"Failed to ensure builtin model providers during startup: {e}")

    # 初始化模型缓存（v2 模型选择使用）
    try:
        from yuxi.models.providers.cache import model_cache
        from yuxi.models.providers.service import get_all_model_providers

        async with pg_manager.get_async_session_context() as session:
            providers = await get_all_model_providers(session)
            model_cache.rebuild(providers)
    except Exception as e:
        logger.error(f"Failed to initialize model cache during startup: {e}")

    # 初始化知识库管理器
    if os.environ.get("LITE_MODE", "").lower() in ("true", "1"):
        logger.info("LITE_MODE enabled, skipping knowledge base initialization")
    else:
        try:
            await knowledge_base.initialize()
        except Exception as e:
            logger.error(f"Failed to initialize knowledge base manager: {e}")

    # 预热 Redis（run 队列）
    try:
        redis = await get_redis_client()
        await redis.ping()
    except Exception as e:
        logger.warning(f"Run queue redis unavailable on startup: {e}")

    # 启动运行时配置同步线程（周期性从 Redis 拉取管理员保存的配置快照）
    config.start_runtime_sync()

    try:
        init_sandbox_provider()
    except Exception as e:
        logger.error(f"Failed to initialize sandbox provider during startup: {e}")

    # =========================================================
    # 2. 核心修复：在这里执行一次 setup()，建完表就拉倒
    # =========================================================
    checkpointer = AsyncPostgresSaver(pg_manager.langgraph_pool)
    await checkpointer.setup()
    print("LangGraph Checkpoint tables verified/created!")

    await tasker.start()
    await retry_coordinator.start()
    try:
        async with pg_manager.get_async_session_context() as session:
            await FeishuKnowledgeRepository(session).reconcile_interrupted_work()
    except Exception as e:
        logger.error(f"Failed to reconcile interrupted Feishu work during startup: {e}")
    try:
        recovered = await _recover_recent_feishu_processing_tasks()
        if recovered:
            logger.info("Recovered {} recent Feishu processing tasks after restart", recovered)
    except Exception as e:
        logger.error(f"Failed to recover interrupted Feishu processing tasks during startup: {e}")
    logger.info(f"""

░██     ░██                       ░██
 ░██   ░██
  ░██ ░██   ░██    ░██ ░██    ░██ ░██
   ░████    ░██    ░██  ░██  ░██  ░██
    ░██     ░██    ░██   ░█████   ░██
    ░██     ░██   ░███  ░██  ░██  ░██
    ░██      ░█████░██ ░██    ░██ ░██  v{get_version()}

    """)
    logger.info("Yuxi backend startup complete")
    yield
    await tasker.shutdown()
    await retry_coordinator.stop()
    shutdown_sandbox_provider()
    await close_queue_clients()
    close_shared_neo4j_connection()
    await pg_manager.close()
