from contextlib import asynccontextmanager
import asyncio
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, agent, catalog, checkout, health
from app.core.config import get_settings
from app.db.database import init_db, resolve_database_url
from app.prompts import PROMPTS_VERSION, loader as prompt_loader
from app.services import audit as audit_service

settings = get_settings()

# 让内部 _logger.info 也能透传到 stderr。
# uvicorn 的 log_config 只配置 uvicorn.*，把 root handlers 清空；
# 业务 logger 冒泡到 root 时无人接，会被静默吞掉。这里补一个 StreamHandler。
_root_logger = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler)
           and getattr(h, "stream", None) is sys.stderr
           for h in _root_logger.handlers):
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    _root_logger.addHandler(_handler)
_root_logger.setLevel(logging.INFO)

# 提示词启动自检：把 app/prompts/*.yaml 全读一遍并校验结构。
#
# 这一步放在模块导入期而不是 lifespan 里，是为了让「YAML 写坏了」在服务
# 接受第一个请求之前就炸出来。缺失字段、少占位符、工具名拼错都会被拦下，
# 错误信息指明具体文件和字段，不用去翻日志猜。
#
# 代价是导入 app.main 就必须有一份完整的提示词——这正是我们要的 fail-fast：
# 提示词跟代码一起提交，它的损坏概率等同于代码损坏，静默降级到旧文案
# 只会制造「改了没生效」的幽灵问题。
prompt_loader.load_all()
prompt_loader.set_hot_reload(settings.PROMPT_HOT_RELOAD)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(seed_if_empty=True)

    from app.services import llm

    print(f"[SmartMart] 数据库：{resolve_database_url()}")
    print(
        f"[SmartMart] 导购大模型："
        f"{'已接入 ' + settings.LLM_MODEL if llm.available(settings) else '未配置，使用本地知识库'}"
    )
    print(
        f"[SmartMart] 视觉大模型："
        f"{'已接入 ' + settings.VISION_MODEL if settings.VISION_API_KEY else '未配置，使用模拟识别'}"
    )
    print(f"[SmartMart] 提示词：{PROMPTS_VERSION}")
    print(
        f"[SmartMart] 提示词热重载："
        f"{'开（改 YAML 免重启）' if settings.PROMPT_HOT_RELOAD else '关（改 YAML 需重启）'}"
    )

    # 自动清理过期审计记录：与 FastAPI app 同寿命，进程关停时一并退出
    stop_event = asyncio.Event()
    purge_task = asyncio.create_task(audit_service.run_purge_loop(stop_event, settings))

    try:
        yield
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(purge_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            purge_task.cancel()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="无人超市智能导购与自适应视觉结账一体化系统 - 后端服务",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(agent.router, prefix="/api/v1", tags=["agent"])
app.include_router(catalog.router, prefix="/api/v1", tags=["catalog"])
app.include_router(checkout.router, prefix="/api/v1", tags=["checkout"])
app.include_router(admin.router, prefix="/api/v1", tags=["admin"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=True)
