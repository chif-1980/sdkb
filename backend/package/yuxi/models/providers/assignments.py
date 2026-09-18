"""两个产品共用的模型用途与保存校验。"""

from yuxi.models.providers.cache import model_cache

MODEL_ROLES = {
    "default_model": "chat",
    "fast_model": "chat",
    "embed_model": "embedding",
    "reranker": "rerank",
    "content_guard_llm_model": "chat",
}


def validate_model_assignments(items: dict) -> None:
    for role, model_type in MODEL_ROLES.items():
        if role not in items:
            continue
        spec = items[role]
        info = model_cache.get_model_info(spec) if isinstance(spec, str) else None
        if not info or info.model_type != model_type:
            raise ValueError(f"{role} 请选择已启用的 {model_type} 模型")
        if not info.api_key:
            raise ValueError(f"模型 {spec} 尚未配置 API Key")
