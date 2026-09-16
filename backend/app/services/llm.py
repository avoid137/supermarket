"""大模型适配层（OpenAI 兼容协议）。

DeepSeek / 通义千问 / OpenAI / 本地 vLLM 等只要兼容 OpenAI 协议都能直接接入，
改 .env 里的 base_url 与 model 即可。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import Settings


# ---------------------------------------------------------------- DSML 过滤器
#
# 一些兼容 OpenAI 协议的网关（Qwen3 / 部分 DeepSeek 兼容层）会把工具调用也
# 同时塞进 `delta.content` 字段里，格式形如：
#     <|DSML|tool_calls|>            （半角竖线）
#     <|invoke name="check_stock"|>
#     <|parameter name="item_name" string="true"|>可口可乐<|/parameter|>
#     <|/invoke|>
#     <|/tool_calls|>
# 或者用全角标点：
#     <｜｜DSML｜｜tool_calls｜＞      （中文输入法下容易输成全角）
#     <｜｜invoke name="check_stock"｜＞
# 这些是模型内部协议标记，不应该让用户看到。我们把它们整段从 content 里
# 删掉，再只把"清洗过的文本"返回给前端 / 模型。
#
# 行为：状态机式丢弃 `<|` ... `|>`（同时支持半角/全角）。
#
# 为什么不用一行正则？因为这玩意儿是流式的，单个 token 可能正好切在 `<|`
# 中间。需要 hold-back 状态机才能正确切。

# 最外层 `<|..|>` 标签：内层 `|` 是允许的（DSML 标记 like `<|DSML|invoke|>`）。
# 用 `[\s\S]*?` + 强制配对 `[|｜][>＞]` 一次性剥除。
_DSML_RE = re.compile(r"<[|｜][\s\S]*?[|｜][>＞]")

# 半角/全角的合法配对字符
_L = ("|", "｜")  # opening | 后的竖线字符
_R = (">", "＞")  # closing | 后跟随的右尖括号


def _is_open_bar(c: str) -> bool:
    return c in _L


def _is_close_bar(c: str) -> bool:
    return c in _L


def _is_close_angle(c: str) -> bool:
    return c in _R


def strip_dsml_text(text: str) -> str:
    """一次性清洗整段文本，丢掉所有 `<|...|>` / `<｜...｜>` 块。"""
    if not text:
        return text
    return _DSML_RE.sub("", text)


async def strip_dsml_stream(
    source: AsyncIterator[str],
) -> AsyncIterator[str]:
    """流式过滤器：丢弃 `<|...|>` / `<｜...｜>` 段，将剩余字符分块透传。

    状态机：
      NORMAL   → 正常累积，遇到 `<` 时把字符 hold-back 一个字符，等下一个字符判定
                 若下一字符是 `|` 或 `｜` → 进入 WAIT_END
                 若下一字符是其它 → 把 `<` 当普通字符输出
      WAIT_END → 处于 `<|...|>` 内部，丢弃所有字符直到匹配 `|>` / `｜＞`（同样 hold-back）

    为什么不用正则？因为这玩意儿是流式的，`<` 单独到达、下一段才到 `|` 的概率
    不低，必须逐字符状态机才能保证跨 chunk 边界匹配。
    """

    NORMAL, WAIT_END = 0, 1
    state = NORMAL
    pending = ""        # 累计未消费的字符
    out: list[str] = [] # 已安全输出的字符缓冲
    flush_size = 8      # 累积多少字符 yield 一次（不必每次 <1 字符都 yield）

    async def _flush() -> str:
        if not out:
            return ""
        chunk = "".join(out)
        out.clear()
        return chunk

    async for piece in source:
        if piece is None:
            continue
        pending += piece

        i = 0
        while i < len(pending):
            c = pending[i]
            if state == NORMAL:
                if c == "<":
                    # < 后面跟着的字符必须确定才能决策
                    if i + 1 >= len(pending):
                        # 当前 chunk 末尾，留给下一 chunk 判断
                        break
                    nxt = pending[i + 1]
                    if _is_open_bar(nxt):
                        # 进入匹配态：丢掉 `<` + 竖线
                        i += 2
                        state = WAIT_END
                        continue
                    # 普通 `<`，不是 `<|...|>` 起始
                    out.append(c)
                    i += 1
                else:
                    out.append(c)
                    i += 1
            else:
                # WAIT_END：吞掉一切直到 `|>` / `｜＞`
                if _is_close_bar(c):
                    if i + 1 >= len(pending):
                        # 当前是竖线但不确定后面是否 >，留给下一 chunk
                        break
                    nxt = pending[i + 1]
                    if _is_close_angle(nxt):
                        i += 2
                        state = NORMAL
                        continue
                    # 不是 `|>`，继续吞
                i += 1

        # 把已消费过的部分裁掉，余下的是 hold-back 的单字符（如果有）
        pending = pending[i:]

        if len(out) >= flush_size:
            yield await _flush()

    # 收尾：循环结束后仍有残余 pending
    if state == NORMAL and pending:
        # 末尾要么是空，要么是单个 `<`（孤立），按普通字符处理即可
        out.append(pending)
    # WAIT_END 残留直接丢弃：流意外结束不应当吐出半段 DSML
    final = await _flush()
    if final:
        yield final


def available(settings: Settings) -> bool:
    return bool(settings.LLM_API_KEY)


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }


def _url(settings: Settings) -> str:
    return f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"


async def chat(
    messages: list[dict[str, Any]],
    settings: Settings,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """非流式调用，用于工具决策。"""
    payload: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
        resp = await client.post(_url(settings), json=payload, headers=_headers(settings))
        resp.raise_for_status()
        data = resp.json()

    message = data["choices"][0]["message"]
    return {
        "content": strip_dsml_text(message.get("content") or ""),
        "tool_calls": message.get("tool_calls") or [],
        # 用量透传：评测集要算「单次导购成本」，没有它就只能估。
        # 部分兼容网关不返回 usage，缺失时给空字典而不是报错。
        "usage": data.get("usage") or {},
    }


async def stream(
    messages: list[dict[str, Any]],
    settings: Settings,
    temperature: float = 0.4,
) -> AsyncIterator[str]:
    """流式调用，逐段产出文本增量（已剥除模型私有 DSML 标记）。"""
    payload: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }

    async def _raw_lines() -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            async with client.stream(
                "POST", _url(settings), json=payload, headers=_headers(settings)
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text

    # 包装一层 DSML 过滤器：模型偶尔会在 delta.content 里塞工具调用标记
    async for clean in strip_dsml_stream(_raw_lines()):
        yield clean
