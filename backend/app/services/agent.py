"""导购 Agent：意图理解 → 工具调用 → 流式作答。

两条执行路径：
  llm   配置了 LLM_API_KEY，由大模型决定调用哪些工具，流式生成自然语言答案
  local 无 Key 时走规则意图识别 + 模板生成，同样流式输出，保证演示不中断

两条路径都强制「答案必须有商品数据支撑」，并回传引用卡片供前端展示。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.core.config import Settings
from app.models.schemas import AskRequest, Citation, Product
from app.prompts import (
    local_raw,
    local_text,
    no_more_tool_calls_note,
    system_prompt,
    tool_names,
    tool_prompt,
)
from app.repositories import product_repo
from app.services import llm
from app.services.text2sql import format_result, run_readonly_query

_logger = logging.getLogger("smartmart.chat_log")

# 提示词已抽到 app/prompts/guide.yaml。
#
# 这里刻意不留模块级常量：常量在 import 时就绑定了，热重载开启后改了 YAML
# 也拿不到新版本。改用模块级 __getattr__ 惰性转发（见本文件末尾），
# 既保住 `from app.services.agent import SYSTEM_PROMPT` 的向后兼容，
# 又能让每次访问都拿到最新文案。

# 工具的 JSON Schema 骨架。文案（description 与参数描述）在 guide.yaml，
# 这里只保留协议结构：参数名、类型、是否必填。
#
# 改动提示：新增工具要同时改这里和 app/prompts/guide.yaml，
# 少改任何一边 loader 的自检都会拦下来。
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_product": {"params": {"keyword": "string"}, "required": ["keyword"]},
    "get_nutrition": {"params": {"keyword": "string"}, "required": ["keyword"]},
    "get_location": {"params": {"keyword": "string"}, "required": ["keyword"]},
    # 查询全部促销，不需要参数
    "get_promotions": {"params": {}, "required": []},
    "recommend_pairing": {"params": {"keyword": "string"}, "required": ["keyword"]},
    "check_stock": {"params": {"keyword": "string"}, "required": ["keyword"]},
    "query_product_database": {
        "params": {"sql": "string", "reason": "string"},
        "required": ["sql"],
    },
}


def build_tools() -> list[dict[str, Any]]:
    """组装发给模型的工具定义（骨架 + YAML 文案）。

    做成函数而不是模块级 TOOLS 常量，原因同上面的提示词：
    热重载要能生效，就不能在 import 时把文案定死。
    """
    tools: list[dict[str, Any]] = []
    for name in tool_names():
        schema = _TOOL_SCHEMAS[name]
        text = tool_prompt(name)

        properties: dict[str, Any] = {}
        for param_name, param_type in schema["params"].items():
            prop: dict[str, Any] = {"type": param_type}
            desc = text["param_desc"].get(param_name, "")
            if desc:
                # 原文里 description 为空的参数就不带这个字段，保持一致
                prop["description"] = desc
            properties[param_name] = prop

        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if schema["required"]:
            parameters["required"] = schema["required"]

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": text["description"],
                    "parameters": parameters,
                },
            }
        )
    return tools



# ---------------------------------------------------------------- 工具实现


def _brief(product: Product) -> dict[str, Any]:
    return {
        "sku_id": product.sku_id,
        "name": product.name,
        "brand": product.brand,
        "category": product.category,
        "spec": product.spec,
        "price": product.price,
        "member_price": product.member_price,
        "location": f"{product.shelf.aisle} 第 {product.shelf.level} 层",
        "location_desc": product.shelf.desc,
        "tags": product.tags,
        "stock": product.stock,
        "promotions": [p.desc for p in product.promotions],
    }


def _pairing_reason(product: Product, candidate: Product) -> str:
    rules = [
        ({"膨化食品"}, {"0糖", "气泡水", "碳酸饮料"}, "解腻解渴，是薯片最经典的搭子"),
        ({"咖啡"}, {"饼干", "糕点"}, "苦甜互补，配一份小点心刚好"),
        ({"巧克力"}, {"无糖茶", "0卡"}, "甜度较高，配无糖茶更清爽"),
        ({"辣味"}, {"0糖", "碳酸饮料", "饮用水"}, "辣味重，配清爽饮品缓解"),
        ({"坚果"}, {"无糖茶", "0卡"}, "坚果偏油，配无糖茶更舒服"),
        ({"冰品"}, {"饼干"}, "冷热搭配，口感层次更丰富"),
    ]
    for src_tags, dst_tags, reason in rules:
        if set(product.tags) & src_tags and set(candidate.tags) & dst_tags:
            return reason
    return f"同为{candidate.category}，常与「{product.name}」一起购买"


def _recommend(product: Product, limit: int = 2) -> list[tuple[Product, str]]:
    scored: list[tuple[float, Product, str]] = []
    for candidate in product_repo.list_products():
        if candidate.sku_id == product.sku_id:
            continue
        reason = _pairing_reason(product, candidate)
        score = 0.0
        if set(product.tags) & {"膨化食品"} and set(candidate.tags) & {"0糖", "气泡水", "碳酸饮料"}:
            score += 60
        elif set(product.tags) & {"咖啡"} and set(candidate.tags) & {"饼干", "糕点"}:
            score += 55
        elif set(product.tags) & {"巧克力"} and set(candidate.tags) & {"无糖茶", "0卡"}:
            score += 50
        elif set(product.tags) & {"辣味"} and set(candidate.tags) & {"0糖", "碳酸饮料", "饮用水"}:
            score += 50
        elif candidate.category == "饮料" and product.category == "零食":
            score += 30
        elif candidate.category == "零食" and product.category == "饮料":
            score += 25
        if candidate.promotions:
            score += 8
        if score > 0:
            scored.append((score, candidate, reason))
    scored.sort(key=lambda t: -t[0])
    return [(p, r) for _, p, r in scored[:limit]]


def run_tool(name: str, arguments: dict[str, Any]) -> tuple[str, list[Citation]]:
    """执行一个工具，返回 (给模型的文本结果, 商品引用列表)。

    关于 citations：
      - 它是「答案引用到的商品卡片」，不是「工具调用链里所有出现过的商品」。
      - get_promotions / recommend_pairing 这种**附带信息**工具的返回商品，
        默认不进 citations（让模型在文本里自然引用即可，不污染前端卡片列表）。
      - 实际去重在 _run_with_llm 末尾统一做；这里只决定要不要产出。
    """
    keyword = str(arguments.get("keyword", "")).strip()

    if name == "query_product_database":
        result = run_readonly_query(str(arguments.get("sql", "")))
        return format_result(result), []

    if name == "get_promotions":
        # 促销工具返回的商品只是给模型参考的"全局促销上下文"，
        # 不应该进 citations（用户没问促销，把全部促销商品列成卡片反而是噪声）。
        # 模型仍可在文本里写"可口可乐正在第二件半价"。
        lines = ["门店当前促销："]
        for promo in product_repo.store_promotions():
            lines.append(f"- {promo.desc}")
        promoted = product_repo.products_with_promotion()
        for product in promoted:
            for promo in product.promotions:
                lines.append(f"- {product.name}（¥{product.price}）：{promo.desc}")
        return "\n".join(lines), []

    # check_stock 的入参以 sku_ids 为准（精确取货，避开同名不同规格）；但工具对模型
    # 暴露的参数是 keyword（见 app/prompts/guide.yaml），模型只会传关键词。
    # 早期实现只读 sku_ids，导致声明与实现错位、库存工具对模型永久不可用——
    # 顾客问「还有货吗」时工具恒返回「请提供 sku_id 列表」。2026-09-16 的 58 条
    # 评测集把它暴露出来（5 道库存题全挂），故补上 keyword → 商品的解析分支：
    # 两条路都保留，显式传 id 仍走精确路径。
    if name == "check_stock":
        sku_ids = arguments.get("sku_ids") or []
        if isinstance(sku_ids, list) and sku_ids:
            products = product_repo.list_products_by_ids([str(s) for s in sku_ids])
        else:
            products = product_repo.search_products(keyword, top_k=3)
        if not products:
            target = keyword or "、".join(str(s) for s in sku_ids) or "该商品"
            return f"未找到「{target}」，请确认商品名或 sku_id 是否正确。", []
        lines = []
        citations = []
        for p in products:
            if p.stock <= 0:
                lines.append(f"{p.name}（{p.spec}）：已售罄，暂时没有库存。")
                reason = "已售罄"
            elif p.stock <= 5:
                lines.append(f"{p.name}（{p.spec}）：仅剩 {p.stock} 件，需要的话建议尽快来。")
                reason = f"库存紧张（剩{p.stock}件）"
            else:
                lines.append(f"{p.name}（{p.spec}）：当前库存 {p.stock} 件，货源充足。")
                reason = "库存充足"
            citations.append(Citation(sku_id=p.sku_id, name=p.name, reason=reason))
        return "\n".join(lines), citations

    products = product_repo.search_products(keyword, top_k=3)
    if not products:
        return f"未在本店找到与「{keyword}」相关的商品。", []

    if name == "get_nutrition":
        lines = []
        for p in products:
            n = p.nutrition
            lines.append(
                f"{p.name}（{p.spec}）每 100 克/毫升：能量 {n.energy_kj}kJ、"
                f"蛋白质 {n.protein_g}g、脂肪 {n.fat_g}g、碳水 {n.carb_g}g、钠 {n.sodium_mg}mg。"
            )
            lines.append(f"配料：{p.ingredients}")
            lines.append(f"过敏原：{'、'.join(p.allergens) if p.allergens else '无标注'}")
        citations = [Citation(sku_id=p.sku_id, name=p.name, reason=local_text("citation_reason.nutrition")) for p in products]
        return "\n".join(lines), citations

    if name == "get_location":
        lines = []
        for p in products:
            lines.append(f"{p.name}：{p.shelf.aisle} 货架第 {p.shelf.level} 层。{p.shelf.desc}。")
        citations = [Citation(sku_id=p.sku_id, name=p.name, reason=local_text("citation_reason.location")) for p in products]
        return "\n".join(lines), citations

    if name == "recommend_pairing":
        main = products[0]
        pairs = _recommend(main)
        if not pairs:
            return f"{main.name}暂未配置搭配推荐。", [Citation(sku_id=main.sku_id, name=main.name, reason=local_text("citation_reason.main"))]
        lines = [f"买「{main.name}」的顾客常搭配："]
        citations = [Citation(sku_id=main.sku_id, name=main.name, reason=local_text("citation_reason.main"))]
        for candidate, reason in pairs:
            lines.append(f"- {candidate.name}（¥{candidate.price}）：{reason}")
            citations.append(Citation(sku_id=candidate.sku_id, name=candidate.name, reason=reason))
        return "\n".join(lines), citations

    # 默认 search_product
    lines = []
    for p in products:
        promo_text = f"，{p.promotions[0].desc}" if p.promotions else ""
        stock_text = "已售罄" if p.stock <= 0 else f"库存 {p.stock} 件"
        lines.append(
            f"{p.name}｜{p.spec}｜¥{p.price}（会员价 ¥{p.member_price}）｜"
            f"{p.shelf.aisle} 第 {p.shelf.level} 层｜{stock_text}{promo_text}"
        )
    citations = [Citation(sku_id=p.sku_id, name=p.name, reason=local_text("citation_reason.search")) for p in products]
    return "\n".join(lines), citations


# ---------------------------------------------------------------- 本地降级路径

ALLERGEN_WORDS = ["花生", "坚果", "牛奶", "小麦", "大豆", "鸡蛋", "麸质", "乳糖"]

INTENT_RULES: list[tuple[str, list[str]]] = [
    ("location", ["在哪", "哪里", "什么位置", "几号架", "货架", "怎么走", "找", "放在"]),
    ("nutrition", ["营养", "热量", "卡路里", "大卡", "糖", "脂肪", "蛋白质", "钠", "成分", "配料"]),
    ("allergen", ["过敏", "过敏原", "不含", "能不能吃", "乳糖", "花生", "坚果", "麸质"]),
    ("promotion", ["促销", "优惠", "打折", "活动", "满减", "第二件", "便宜"]),
    ("pairing", ["搭配", "推荐", "一起", "配什么", "组合", "搭子"]),
    ("price", ["多少钱", "价格", "贵不贵", "几块"]),
    ("stock", ["库存", "还有货", "有货吗", "有货么", "没货", "售罄", "还剩", "还剩几", "剩多少", "买几件", "买多少"]),
]


def detect_intent(question: str) -> str:
    q = question.lower()
    for intent, keywords in INTENT_RULES:
        if any(k in q for k in keywords):
            return intent
    return "general"


def _local_answer(question: str, intent: str) -> tuple[str, list[Citation]]:
    """无大模型时基于规则生成答案。

    话术在 app/prompts/local_fallback.yaml。只抽了静态措辞和模板串，
    循环与条件判断留在代码里——把控制流搬进 YAML 会让两边都更难读。

    引用卡片的标签（citation_reason.*）与 LLM 路径共用同一份，
    这样改一次 YAML，两种模式的展示口径就一起变了。
    """
    # 从问题中尽可能提取商品关键词：优先匹配到具体商品
    products = product_repo.search_products(question, top_k=3)
    main = products[0] if products else None

    if intent == "promotion" or (main is None and intent == "general"):
        promoted = product_repo.products_with_promotion()[:4]
        lines = [local_text("promotion.header")]
        for promo in product_repo.store_promotions():
            lines.append(local_text("promotion.store_wide", desc=promo.desc))
        for p in promoted:
            promo_text = "、".join(x.desc for x in p.promotions)
            lines.append(
                local_text(
                    "promotion.item", name=p.name, price=p.price, promo_text=promo_text
                )
            )
        citations = [
            Citation(
                sku_id=p.sku_id,
                name=p.name,
                reason=local_text("citation_reason.promotion"),
            )
            for p in promoted
        ]
        return "\n".join(lines), citations

    if main is None:
        # not_found 在 YAML 里就是个两行列表，整段拼回去
        return "\n".join(local_raw("not_found")), []

    if intent == "location":
        text = local_text(
            "location",
            name=main.name,
            aisle=main.shelf.aisle,
            level=main.shelf.level,
            desc=main.shelf.desc,
            price=main.price,
            member_price=main.member_price,
        )
        return text, [
            Citation(
                sku_id=main.sku_id,
                name=main.name,
                reason=local_text("citation_reason.location"),
            )
        ]

    if intent == "allergen":
        targets = [a for a in ALLERGEN_WORDS if a in question]
        safe = product_repo.filter_excluding_allergens(targets)
        if safe:
            label = "、".join(targets)
            lines = [local_text("allergen.header", label=label, count=len(safe))]
            for p in safe[:3]:
                lines.append(
                    local_text(
                        "allergen.item",
                        name=p.name,
                        price=p.member_price,
                        aisle=p.shelf.aisle,
                        level=p.shelf.level,
                    )
                )
            lines.append(local_text("allergen.disclaimer"))
            citations = [
                Citation(
                    sku_id=p.sku_id,
                    name=p.name,
                    reason=local_text("citation_reason.allergen_free", label=label),
                )
                for p in safe[:3]
            ]
            return "\n".join(lines), citations

    if intent == "stock":
        # 与 LLM 路径的 check_stock 工具保持同一套口径（售罄/紧张/充足），
        # 避免「大模型说有货、本地模式说没货」这类自相矛盾
        lines = []
        citations = []
        for p in products:
            if p.stock <= 0:
                lines.append(local_text("stock.out", name=p.name, spec=p.spec))
                reason = local_text("citation_reason.stock_out")
            elif p.stock <= 5:
                lines.append(
                    local_text("stock.low", name=p.name, spec=p.spec, count=p.stock)
                )
                reason = local_text("citation_reason.stock_low", count=p.stock)
            else:
                lines.append(
                    local_text("stock.ok", name=p.name, spec=p.spec, count=p.stock)
                )
                reason = local_text("citation_reason.stock_ok")
            citations.append(Citation(sku_id=p.sku_id, name=p.name, reason=reason))
        return "\n".join(lines), citations

    if intent == "nutrition":
        n = main.nutrition
        text = "\n".join(
            [
                local_text("nutrition.header", name=main.name),
                local_text("nutrition.energy", value=n.energy_kj),
                local_text("nutrition.protein", value=n.protein_g),
                local_text("nutrition.fat", value=n.fat_g),
                local_text("nutrition.carb", value=n.carb_g),
                local_text("nutrition.sodium", value=n.sodium_mg),
                local_text("nutrition.ingredients", value=main.ingredients),
                local_text(
                    "nutrition.allergens",
                    value="、".join(main.allergens)
                    if main.allergens
                    else local_text("nutrition.allergens_empty"),
                ),
            ]
        )
        return text, [
            Citation(
                sku_id=main.sku_id,
                name=main.name,
                reason=local_text("citation_reason.nutrition"),
            )
        ]

    if intent == "pairing":
        pairs = _recommend(main)
        main_citation = Citation(
            sku_id=main.sku_id,
            name=main.name,
            reason=local_text("citation_reason.main"),
        )
        if not pairs:
            return local_text("pairing.empty", name=main.name), [main_citation]
        lines = [local_text("pairing.header", name=main.name)]
        citations = [main_citation]
        for candidate, reason in pairs:
            lines.append(
                local_text(
                    "pairing.item",
                    name=candidate.name,
                    price=candidate.price,
                    reason=reason,
                )
            )
            citations.append(
                Citation(sku_id=candidate.sku_id, name=candidate.name, reason=reason)
            )
        return "\n".join(lines), citations

    if intent == "price":
        promo_text = f"，{main.promotions[0].desc}" if main.promotions else ""
        text = local_text(
            "price",
            name=main.name,
            spec=main.spec,
            price=main.price,
            member_price=main.member_price,
            promo=promo_text,
            aisle=main.shelf.aisle,
            level=main.shelf.level,
        )
        return text, [
            Citation(
                sku_id=main.sku_id,
                name=main.name,
                reason=local_text("citation_reason.price"),
            )
        ]

    promo_text = (
        local_text("misc.promo_prefix", desc=promo_text_desc(main))
        if main.promotions
        else ""
    )
    stock_text = (
        local_text("misc.stock_out")
        if main.stock <= 0
        else local_text("misc.stock_count", count=main.stock)
    )
    text = local_text(
        "detail",
        name=main.name,
        spec=main.spec,
        price=main.price,
        member_price=main.member_price,
        promo=promo_text,
        aisle=main.shelf.aisle,
        level=main.shelf.level,
        desc=main.shelf.desc,
        stock=stock_text,
        tags="、".join(main.tags) if main.tags else local_text("misc.tags_empty"),
    )
    return text, [
        Citation(
            sku_id=main.sku_id,
            name=main.name,
            reason=local_text("citation_reason.detail"),
        )
    ]


def promo_text_desc(product: Product) -> str:
    return "、".join(p.desc for p in product.promotions)


async def _emit_stream(text: str, chunk_size: int = 3, delay: float = 0.012) -> AsyncIterator[str]:
    """把整段文本切成小块，模拟打字机效果。"""
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if delay:
            await asyncio.sleep(delay)


# ---------------------------------------------------------------- 对话日志
#
# 后台看板需要展示「最近对话抽样」。流式响应里把所有 delta 都写库会让行数
# 爆炸（每句话几十条），所以只写**两行**：user 行 + assistant 行（聚合后的全文）。
# 失败降级：写库异常被吞掉，不阻塞主链路 SSE。
#
# 用同步 session_scope() 写在独立线程里，避免在事件循环里同步阻塞。
_log_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chat-log")


def _persist_chat_log(
    session_id: str | None,
    user_text: str,
    assistant_text: str,
    citations: list[Citation],
    mode: str,
    intent: str,
) -> None:
    """把一轮问询落成 user + assistant 两行。失败不抛错。"""
    try:
        from app.db.database import now_ts, session_scope
        from app.db.models import ChatLogModel

        now = now_ts()
        with session_scope() as db:
            db.add(
                ChatLogModel(
                    session_id=session_id,
                    role="user",
                    content=user_text,
                    mode=mode,
                    intent=intent,
                    created_at=now,
                )
            )
            db.add(
                ChatLogModel(
                    session_id=session_id,
                    role="assistant",
                    content=assistant_text,
                    citations=[c.model_dump() for c in citations],
                    mode=mode,
                    intent=intent,
                    created_at=now,
                )
            )
    except Exception as exc:  # 写库失败不阻塞主链路，仅打 warning
        _logger.warning("chat log persist failed: %s", exc)


def _schedule_chat_log(
    session_id: str | None,
    user_text: str,
    assistant_text: str,
    citations: list[Citation],
    mode: str,
    intent: str,
) -> None:
    """非阻塞提交到后台线程，避免影响 SSE 流式响应。"""
    try:
        _log_executor.submit(
            _persist_chat_log,
            session_id,
            user_text,
            assistant_text,
            citations,
            mode,
            intent,
        )
    except RuntimeError:
        # 进程关闭时 executor 已 shutdown → 直接放弃
        _logger.warning("chat log executor not available, skip")


# ---------------------------------------------------------------- 主入口


async def run_agent(request: AskRequest, settings: Settings) -> AsyncIterator[dict[str, Any]]:
    started = time.perf_counter()
    intent = detect_intent(request.question)
    history = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in request.history[-6:]]

    use_llm = request.use_llm and llm.available(settings)
    mode = "llm" if use_llm else "local"
    yield {"type": "meta", "mode": mode, "intent": intent}

    # 收集 assistant 最终正文 + citations，done 之前异步落库
    assistant_buf: list[str] = []
    captured_citations: list[Citation] = []

    try:
        if use_llm:
            async for event in _run_with_llm(request, settings, history):
                _capture_for_log(event, assistant_buf, captured_citations)
                yield event
        else:
            async for event in _run_local(request, intent):
                _capture_for_log(event, assistant_buf, captured_citations)
                yield event
    except Exception as exc:  # 大模型不可用时静默降级，保证服务可用
        async for event in _run_local(request, intent):
            _capture_for_log(event, assistant_buf, captured_citations)
            yield event
        yield {"type": "notice", "text": f"大模型调用失败，已切换本地知识库：{type(exc).__name__}"}

    # 落库：失败降级，不影响 done 事件
    _schedule_chat_log(
        session_id=request.session_id,
        user_text=request.question,
        assistant_text="".join(assistant_buf),
        citations=captured_citations,
        mode=mode,
        intent=intent,
    )

    yield {"type": "done", "latency_ms": int((time.perf_counter() - started) * 1000)}


def _capture_for_log(
    event: dict[str, Any],
    assistant_buf: list[str],
    citations: list[Citation],
) -> None:
    """从流式事件里收集 assistant 文本和 citations，落库前聚合用。"""
    t = event.get("type")
    if t == "delta":
        assistant_buf.append(event.get("text") or "")
    elif t == "citations":
        for c in event.get("items", []) or []:
            citations.append(
                Citation(
                    sku_id=c.get("sku_id", ""),
                    name=c.get("name", ""),
                    reason=c.get("reason", ""),
                )
            )


async def _run_with_llm(
    request: AskRequest, settings: Settings, history: list[dict[str, str]]
) -> AsyncIterator[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        *history,
        {"role": "user", "content": request.question},
    ]

    result = await llm.chat(messages, settings, tools=build_tools())
    raw_citations: list[Citation] = []

    if result["tool_calls"]:
        tool_messages: list[dict[str, Any]] = []
        for call in result["tool_calls"]:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            content, cites = run_tool(name, arguments)
            raw_citations.extend(cites)
            tool_messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})

        citations = _dedupe_citations(raw_citations)

        if citations:
            yield {"type": "citations", "items": [c.model_dump() for c in citations]}

        messages.append({"role": "assistant", "content": None, "tool_calls": result["tool_calls"]})
        messages.extend(tool_messages)
        # 追加一段 system 级别的硬约束，禁止第二轮再调工具 + 防止 DSML 标签泄漏
        messages.append({"role": "system", "content": no_more_tool_calls_note()})
        async for delta in llm.stream(messages, settings):
            yield {"type": "delta", "text": llm.strip_dsml_text(delta)}
    else:
        # 首轮就直接给了 content —— 仍然过一遍标记剥离兜底
        cleaned = llm.strip_dsml_text(result["content"])
        async for delta in _emit_stream(cleaned):
            yield {"type": "delta", "text": delta}


# ---------------------------------------------------------------- 引用去重
#
# 为什么需要去重？
#   用户问"对比 A 和 B"时，模型会多次调用工具，每次工具都可能产出 A/B 的引用：
#     search_product("A")  → citations: [A, A, A]
#     search_product("B")  → citations: [B, B, B]
#     get_nutrition("A")   → citations: [A]
#     get_nutrition("B")   → citations: [B]
#     check_stock([A, B])  → citations: [A, B]
#   合并后会出现"A × 3、B × 3"等噪声卡片。
#
# 去重策略：按 sku_id 合并，每个 SKU 只保留一个最"具体"的 reason。
#   优先级（数字越小越优先，胜出）：
#     1) 已售罄 / 库存紧张 / 库存充足   —— 与购买决策直接相关
#     2) 价格 / 营养成分 / 货架位置     —— 用户最关心的属性
#     3) 主商品 / 搜索结果 / 商品详情   —— 兜底
#     4) 参与促销 / 搭配推荐 / 其它     —— 辅助信息
#
# 为什么不让 get_promotions 产出 citations 也要去重？
#   答：get_promotions 改完不再产出，所以剩下最大来源是 search_product × N 次
#       + get_nutrition × N 次。这种结构化去重后，每个用户提名的商品只出现 1 次。
_REASON_PRIORITY = {
    "已售罄": 1,
    "库存紧张": 1,
    "库存充足": 1,
    "价格": 2,
    "营养成分": 2,
    "货架位置": 2,
    "主商品": 3,
    "搜索结果": 3,
    "商品详情": 3,
}


def _dedupe_citations(citations: list[Citation]) -> list[Citation]:
    """按 sku_id 去重，每个 SKU 保留 reason 优先级最高的引用。"""
    best: dict[str, Citation] = {}
    for c in citations:
        prev = best.get(c.sku_id)
        if prev is None:
            best[c.sku_id] = c
            continue
        # 数字越小越优先
        prev_pri = _REASON_PRIORITY.get(prev.reason, 4)
        cur_pri = _REASON_PRIORITY.get(c.reason, 4)
        if cur_pri < prev_pri:
            best[c.sku_id] = c
    # 保留首次出现的顺序，避免顺序抖动
    seen: set[str] = set()
    ordered: list[Citation] = []
    for c in citations:
        if c.sku_id in seen:
            continue
        seen.add(c.sku_id)
        ordered.append(best[c.sku_id])
    return ordered


async def _run_local(request: AskRequest, intent: str) -> AsyncIterator[dict[str, Any]]:
    text, citations = _local_answer(request.question, intent)
    if citations:
        yield {"type": "citations", "items": [c.model_dump() for c in citations]}
    async for delta in _emit_stream(text):
        yield {"type": "delta", "text": delta}


def __getattr__(name: str):
    """向后兼容转发（PEP 562）。

    提示词搬到 app/prompts/ 之后，旧代码若仍从本模块 import
    SYSTEM_PROMPT / _NO_MORE_TOOL_CALLS_NOTE / TOOLS 依然可用，
    且每次访问都拿最新值——热重载开启后改了 YAML 这边也跟得上。

    用惰性转发而非模块级常量，正是因为常量在 import 时就定死了。
    模块级 __getattr__ 只在属性查找失败时触发，不影响真实定义。
    """
    if name == "SYSTEM_PROMPT":
        return system_prompt()
    if name == "_NO_MORE_TOOL_CALLS_NOTE":
        return no_more_tool_calls_note()
    if name == "TOOLS":
        return build_tools()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
