from fastapi import APIRouter

from app.core.config import Settings, get_settings
from app.db.database import resolve_database_url
from app.repositories import product_repo
from app.services import llm
from app.services.vision import get_vision_error

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    settings: Settings = get_settings()
    # 密钥配了不等于能用：鉴权失败、额度耗尽都会被拒。
    # 这里一并把最近一次真实调用的报错带出去，避免健康检查掩盖故障。
    vision_error = get_vision_error()
    vision_ok = bool(settings.VISION_API_KEY) and vision_error is None

    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "product_count": len(product_repo.list_products()),
        "database": resolve_database_url(),
        "llm_enabled": llm.available(settings),
        "llm_model": settings.LLM_MODEL if llm.available(settings) else None,
        "vision_enabled": vision_ok,
        "vision_model": settings.VISION_MODEL if vision_ok else None,
        "vision_error": vision_error,
    }
