import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.models.schemas import AskRequest
from app.services import agent as agent_service

router = APIRouter()


@router.post("/agent/ask")
async def ask(request: AskRequest) -> StreamingResponse:
    """导购问答，SSE 流式返回。

    事件类型：
      meta      —— 本次执行模式（llm / local）与识别到的意图
      citations —— 答案引用的商品卡片
      delta     —— 文本增量
      notice    —— 降级提示
      done      —— 结束，附带耗时
    """
    settings = get_settings()

    async def event_stream():
        async for event in agent_service.run_agent(request, settings):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
