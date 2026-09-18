"""Create a source-owned knowledge base and name it after authorization."""

from yuxi import config
from yuxi.knowledge.runtime import knowledge_base
from yuxi.models.providers.cache import model_cache
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository


async def create_feishu_target(source_id: str, user) -> str:
    model = model_cache.get_model_info(config.embed_model)
    if model is None or model.model_type != "embedding":
        raise ValueError("平台默认索引模型不可用，请在模型管理中配置默认 Embedding 模型后重试，或选择已有知识库")
    target = await knowledge_base.create_database(
        f"飞书知识库（待授权-{source_id[:8]}）",
        "飞书知识加工的正式知识库，内容审核通过后发布。",
        kb_type="milvus",
        embedding_model_spec=config.embed_model,
        created_by=user.uid,
        created_by_department_id=user.department_id,
        share_config={"access_level": "user", "user_uids": [user.uid], "department_ids": []},
        auto_generate_questions=False,
        feishu_source_id=source_id,
        feishu_name_pending=True,
    )
    return target["kb_id"]


async def name_feishu_target(source, title: str) -> None:
    target = await KnowledgeBaseRepository().get_by_kb_id(source.target_kb_id)
    if target is None:
        raise ValueError("目标知识库不存在，请重新添加数据源")
    metadata = target.additional_params or {}
    if metadata.get("feishu_source_id") != source.source_id or not metadata.get("feishu_name_pending"):
        return
    if not title.strip():
        raise ValueError("飞书尚未返回知识库名称，请稍后重新检查连接")
    name = title.strip()[:255]
    if target.name != name and await knowledge_base.database_name_exists(name):
        name = f"{name[:240]}（{source.source_id[:8]}）"
    await knowledge_base.update_database(
        source.target_kb_id, name, target.description,
        additional_params={"feishu_name_pending": False},
    )
