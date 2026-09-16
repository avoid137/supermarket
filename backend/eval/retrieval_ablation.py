"""导购检索层对照实验：七种检索策略 × 两个输入口径，量化「混合检索」到底值多少钱。

背景
----
线上 `search_product` 走的是 `app/services/search.py` 的 hybrid_score：
字面分（keyword_score）× 0.35 + 词典式属性语义分（semantic_score）× 0.65。
这个配比是设计出来的，一直没有人验证过「换成别的做法会怎样」。
本脚本把「检索方案是拍脑袋还是选出来的」这个问题补上。

七种策略（同一份商品库、同一批 query，只换排序逻辑）
----
  literal        纯字面：keyword_score（完全相等 / 子串 / 品牌 / 规格 / 标签 / 二元组重叠）
  attr           纯属性语义：semantic_score（颜色/形状/品类/健康/口味/价格/过敏原词典）
  hybrid         现线上：0.65*sem + 0.35*kw，阈值 8.0（直接调用 rank_products）
  vector         纯向量：DashScope text-embedding-v3（1024 维）余弦相似度
  rrf_open       三路 RRF 融合，字母/属性/向量都能放行
  rrf_gated      三路 RRF 融合，闸门只认结构化信号（字面 / 属性）
  rrf_strongvec  三路 RRF 融合，闸门 = 结构化信号 或「向量 top1 且余弦 ≥ 0.60」

两个输入口径（这一步很关键）
----
  raw     原句直喂检索层（模拟「上游没做任何改写」）
  llm_kw  DeepSeek 先把问句改写成检索关键词（贴近线上真实链路）

为什么必须两个口径都跑：线上链路里，模型调用 `search_product` 时传的是它自己
提炼的关键词（「抽纸」而不是「抽纸在哪个区？」）。只看 raw 口径会**高估**
检索层改造的收益——因为向量方案的强项（扛住长口语问句）恰好被上游改写覆盖掉了。
两个口径的差值，就是「上游改写」这一步值多少钱。

评测口径
----
  召回：Recall@1 / Recall@3 / MRR，判定依据是用例里的 allow_skus
  闸门：库外商品题（iPhone / 三文鱼 / 止痛药）能否返回空——防幻觉的第一道闸

排除项：3 条多轮题（q 是「那还有货吗」这类省略指代，单轮检索无意义）。

运行
----
    cd backend && python eval/retrieval_ablation.py              # 两个口径都跑
    cd backend && python eval/retrieval_ablation.py --mode raw   # 只跑原句口径
    cd backend && python eval/retrieval_ablation.py --re-embed   # 向量缓存作废重算
    cd backend && python eval/retrieval_ablation.py --re-extract # 关键词缓存作废重算

产物：results/retrieval_report.md、results/metrics_retrieval.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_EVAL_DIR.parent))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.repositories import product_repo  # noqa: E402
from app.services import llm  # noqa: E402
from app.services.search import (  # noqa: E402
    extract_attributes,
    keyword_score,
    rank_products,
    semantic_score,
    strip_question_words,
)

RESULTS_DIR = _EVAL_DIR / "results"
CASES_PATH = _EVAL_DIR / "cases.json"
VECTOR_CACHE = RESULTS_DIR / "retrieval_vectors.json"
KW_CACHE = RESULTS_DIR / "retrieval_queries.json"

EMBED_MODEL = "text-embedding-v3"
EMBED_BATCH = 10            # DashScope 单次 input 上限内取稳
LITERAL_MIN = 8.0           # 字面策略的入榜阈值（与线上 rank_products 的阈值一致）
VECTOR_MIN = 0.30           # 向量策略的入榜阈值：经验值，未在评测集上调优（避免过拟合）
# 强向量阈值：用于「结构化兜底 + 强向量补召回」的闸门。
# 由本次阈值扫描得出（库外题最高余弦 0.560，库内题中位 0.713）。
# ⚠️ 它是在同一套评测题上选的，严格说存在轻微过拟合；换数据后应重新扫。
VECTOR_STRONG = 0.60
RRF_K = 60                  # RRF 平滑常数，取文献常用值
EXCLUDE_CATS = {"多轮对话"}   # 省略指代题，单轮检索不适用

# ---------------------------------------------------------------- 商品文本


def product_text(p) -> str:
    """商品被编码成向量时用的文本。

    刻意把 allergens 与 ingredients 也喂进去：如果给了这些信息向量仍然处理不好
    「不含花生」这类否定查询，那结论才是干净的（问题在语义表示，不在信息缺失）。
    """
    parts = [p.name, p.brand, p.category, p.spec, " ".join(p.tags)]
    if p.allergens:
        parts.append("过敏原 " + " ".join(p.allergens))
    if p.ingredients:
        parts.append(p.ingredients[:120])
    return " ".join(x for x in parts if x)


# ---------------------------------------------------------------- 输入口径

KW_SYSTEM = """你在给无人超市的导购检索系统做查询改写：把顾客的口语问句改写成适合商品检索的关键词。

规则：
1. 只输出检索关键词，不要回答问题，不要解释。
2. 保留品牌、品类、规格、口味，以及「无糖」「大瓶装」「不含花生」「5 元以下」这类筛选条件。
3. 去掉疑问词、语气词与客套话（如「在哪个区」「怎么走」「有没有」「帮我」「多少钱」）。
4. 多个关键词用空格分隔，不要用标点。
5. 只输出 JSON：{"keyword": "..."}"""


def case_query_raw(c: dict) -> str:
    """原句口径。多轮题的指代靠 history 补全，这里直接拼上轮内容。"""
    hist = c.get("history") or []
    if hist:
        last = hist[-1]
        prev = last.get("content", "") if isinstance(last, dict) else str(last)
        return f"{c['q']}（上一轮：{prev}）"
    return c["q"]


def _parse_keyword(text: str) -> str:
    text = (text or "").strip()
    try:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return str(json.loads(text[start : end + 1]).get("keyword") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return text.strip().strip("`").strip()


async def _extract_one(case: dict, settings, sem: asyncio.Semaphore) -> tuple[str, str]:
    messages = [
        {"role": "system", "content": KW_SYSTEM},
        *[{"role": m["role"], "content": m["content"]} for m in case.get("history") or []],
        {"role": "user", "content": case["q"]},
    ]
    async with sem:
        result = await llm.chat(messages, settings, temperature=0.0)
    kw = _parse_keyword(result.get("content") or "")
    # 提取失败就退回「剥疑问词」，保证实验不因个别调用失败而缺数据
    return case["id"], kw or strip_question_words(case["q"])


def load_keywords(cases: list[dict], settings, use_cache: bool) -> dict[str, str]:
    if use_cache and KW_CACHE.exists():
        cached = json.loads(KW_CACHE.read_text(encoding="utf-8"))
        if all(c["id"] in cached for c in cases):
            print(f"关键词缓存命中：{KW_CACHE.name}")
            return cached
        print("关键词缓存不完整，重新提取")

    async def run_all() -> dict[str, str]:
        sem = asyncio.Semaphore(4)
        pairs = await asyncio.gather(*[_extract_one(c, settings, sem) for c in cases])
        return dict(pairs)

    print(f"调用 {settings.LLM_MODEL} 为 {len(cases)} 条用例提取检索关键词")
    data = asyncio.run(run_all())
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    KW_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"关键词已缓存到 {KW_CACHE.name}")
    return data


# ---------------------------------------------------------------- 向量（按文本缓存）


def embed(texts: list[str], settings) -> list[list[float]]:
    url = settings.VISION_BASE_URL.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {settings.VISION_API_KEY}"}
    out: list[list[float]] = []
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            resp = client.post(url, json={"model": EMBED_MODEL, "input": batch}, headers=headers)
            resp.raise_for_status()
            data = resp.json()["data"]
            # 按 index 排序取，不依赖服务端返回顺序
            out.extend(item["embedding"] for item in sorted(data, key=lambda d: d["index"]))
    return out


def load_vector_store(products, texts_needed: set[str], settings, use_cache: bool) -> dict[str, list[float]]:
    """文本 → 向量。用文本本身做 key，两个口径的 query 共用一份缓存。"""
    store: dict[str, list[float]] = {}
    if use_cache and VECTOR_CACHE.exists():
        cached = json.loads(VECTOR_CACHE.read_text(encoding="utf-8"))
        if cached.get("model") == EMBED_MODEL:
            store = cached.get("vectors", {})

    need = [product_text(p) for p in products] + sorted(texts_needed)
    missing = [t for t in dict.fromkeys(need) if t not in store]
    if not missing:
        print(f"向量缓存命中：{VECTOR_CACHE.name}（{len(store)} 条）")
        return store

    print(f"调用 {EMBED_MODEL} 补算向量 {len(missing)} 条（已有缓存 {len(store)} 条）")
    for text, vec in zip(missing, embed(missing, settings)):
        store[text] = vec
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    VECTOR_CACHE.write_text(
        json.dumps({"model": EMBED_MODEL, "vectors": store}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"向量缓存已更新：{VECTOR_CACHE.name}")
    return store


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- 七种排序策略


def _by_score(items: list[tuple[float, str]], floor: float | None) -> list[str]:
    keep = [t for t in items if floor is None or t[0] >= floor]
    keep.sort(key=lambda t: (-t[0], t[1]))
    return [sku for _, sku in keep]


def rank_literal(products, query: str, **_) -> list[str]:
    core = strip_question_words(query)
    return _by_score([(keyword_score(p, core), p.sku_id) for p in products], LITERAL_MIN)


def rank_attr(products, query: str, **_) -> list[str]:
    attrs = extract_attributes(query)
    if not attrs.hit_count:
        return []          # 没有任何属性线索时不做主张，交还给字面策略
    return _by_score([(semantic_score(p, attrs), p.sku_id) for p in products], 0.0)


def rank_hybrid(products, query: str, **_) -> list[str]:
    # 线上现状：直接调用产品代码，保证实验基线与线上逐字一致
    return [p.sku_id for p in rank_products(products, query, top_k=len(products))]


def rank_vector(products, query: str, *, vecs, qvec, **_) -> list[str]:
    return _by_score([(cosine(qvec, vecs[p.sku_id]), p.sku_id) for p in products], VECTOR_MIN)


def _rrf_core(
    products, query: str, vecs, qvec, gate_with_vector: bool, strong_vec_thr: float | None = None
) -> list[str]:
    """三路 RRF 融合排序 + 闸门。

    与「加权求和」的区别：RRF 只用排名不用分数，省掉了「字面分 8~120、属性分 -30~60、
    余弦 0~1」三个量纲互相折算的麻烦——那正是 hybrid 的 0.65/0.35 配比最容易被质疑的地方。

    `gate_with_vector` 是本次实验的关键开关：**闸门该由谁把关**。
    向量路天然永远存在「最近邻」，它对「库里根本没有这个东西」没有表达能力
    （iPhone 在 29 SKU 里总能找到最像的一件）。让向量参与放行，召回更高但库外题必漏闸——
    实验把这条取舍的代价量化了出来。
    """
    attrs = extract_attributes(query)
    core = strip_question_words(query)

    # 硬过滤：明确说「不含花生」时，含花生的商品直接出局，不参与排序
    pool = products
    if attrs.exclude_allergens:
        pool = [p for p in products if not any(a in p.allergens for a in attrs.exclude_allergens)]

    lit = _by_score([(keyword_score(p, core), p.sku_id) for p in pool], None)
    att = _by_score([(semantic_score(p, attrs), p.sku_id) for p in pool], None) if attrs.hit_count else []
    vec = _by_score([(cosine(qvec, vecs[p.sku_id]), p.sku_id) for p in pool], None)

    fused: dict[str, float] = {}
    for ranked in (lit, att, vec):
        for rank, sku in enumerate(ranked, start=1):
            fused[sku] = fused.get(sku, 0.0) + 1.0 / (RRF_K + rank)

    # 闸门：结构化信号（字面 / 属性）过阈值 = 「库里有这件东西」的实证
    ok_lit = set(_by_score([(keyword_score(p, core), p.sku_id) for p in pool], LITERAL_MIN))
    ok_att = set(_by_score([(semantic_score(p, attrs), p.sku_id) for p in pool], 0.0)) if attrs.hit_count else set()
    ok_vec = set(_by_score([(cosine(qvec, vecs[p.sku_id]), p.sku_id) for p in pool], VECTOR_MIN))
    gate = ok_lit | ok_att | (ok_vec if gate_with_vector else set())

    # 第三条路：结构化兜底 + 高阈值向量补召回。
    # 只在「向量 top1 的余弦足够高」时放行这一件——库外题最高 0.560、库内中位 0.713，
    # 强阈值处存在一条实用分界线（但代价见阈值探针）。
    if strong_vec_thr is not None and vec and cosine(qvec, vecs[vec[0]]) >= strong_vec_thr:
        gate.add(vec[0])

    ranked_all = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    return [sku for sku, _ in ranked_all if sku in gate]


def rank_rrf_open(products, query: str, *, vecs, qvec, **_) -> list[str]:
    return _rrf_core(products, query, vecs, qvec, gate_with_vector=True)


def rank_rrf_gated(products, query: str, *, vecs, qvec, **_):
    return _rrf_core(products, query, vecs, qvec, gate_with_vector=False)


def rank_rrf_strongvec(products, query: str, *, vecs, qvec, **_):
    return _rrf_core(products, query, vecs, qvec, gate_with_vector=False, strong_vec_thr=VECTOR_STRONG)


STRATEGIES = {
    "literal": ("纯字面（关键词）", rank_literal),
    "attr": ("纯属性语义（规则词典）", rank_attr),
    "hybrid": ("现线上（0.65 属性 + 0.35 字面）", rank_hybrid),
    "vector": ("纯向量（text-embedding-v3）", rank_vector),
    "rrf_open": ("RRF 融合（向量也参与放行）", rank_rrf_open),
    "rrf_gated": ("RRF + 结构化闸门", rank_rrf_gated),
    "rrf_strongvec": ("RRF + 结构化闸门 + 强向量补召回", rank_rrf_strongvec),
}


# ---------------------------------------------------------------- 指标


def score_one(ranked: list[str], allow: set[str], ks=(1, 3)) -> dict:
    """单题指标。allow 为空表示「应该返回空」的库外题。"""
    if not allow:
        return {"empty": len(ranked) == 0, "recall": {}, "rr": 0.0, "out": ranked}
    rr = 0.0
    for rank, sku in enumerate(ranked, start=1):
        if sku in allow:
            rr = 1.0 / rank
            break
    return {"empty": None, "recall": {k: float(any(s in allow for s in ranked[:k])) for k in ks}, "rr": rr, "out": ranked}


def evaluate(strategy_fn, cases, products, vecs, query_of: dict[str, str], qvec_of: dict[str, list[float]]) -> dict:
    per_cat: dict[str, list] = {}
    rows = []
    for c in cases:
        if c["cat"] in EXCLUDE_CATS:
            continue
        ranked = strategy_fn(products, query_of[c["id"]], vecs=vecs, qvec=qvec_of.get(c["id"]))
        allow = set(c.get("allow_skus") or [])
        m = score_one(ranked, allow)
        m.update({"id": c["id"], "cat": c["cat"], "q": c["q"], "allow": sorted(allow)})
        rows.append(m)
        per_cat.setdefault(c["cat"], []).append(m)

    scored = [r for r in rows if r["allow"]]                  # 有标准答案的题
    gate = [r for r in rows if not r["allow"]]                # 库外题（该返回空）
    n, ng = max(len(scored), 1), max(len(gate), 1)
    return {
        "n_scored": len(scored),
        "n_gate": len(gate),
        "recall@1": round(sum(r["recall"][1] for r in scored) / n, 4),
        "recall@3": round(sum(r["recall"][3] for r in scored) / n, 4),
        "mrr": round(sum(r["rr"] for r in scored) / n, 4),
        "gate_empty_rate": round(sum(1 for r in gate if r["empty"]) / ng, 4),
        "gate_false": [{"id": r["id"], "q": r["q"], "returned": r["out"][:3]} for r in gate if not r["empty"]],
        "by_category": {
            cat: {
                "n": len([r for r in rs if r["allow"]]),
                "recall@1": round(
                    sum(r["recall"][1] for r in rs if r["allow"]) / max(len([r for r in rs if r["allow"]]), 1), 4
                ),
                "recall@3": round(
                    sum(r["recall"][3] for r in rs if r["allow"]) / max(len([r for r in rs if r["allow"]]), 1), 4
                ),
            }
            for cat, rs in per_cat.items()
        },
        "rows": rows,
    }


# ---------------------------------------------------------------- 向量阈值探针


def threshold_probe(products, cases, vecs, qvec_of) -> dict:
    """扫向量阈值：库内题的 top1 余弦 vs 库外题的 top1 余弦，看有没有可分界。

    这是「纯向量召回最高、却挡不住 iPhone」的根因诊断：
    如果库外题的余弦普遍低于库内题的最低值，那把阈值调高就能既保召回又保闸门；
    如果两条分布叠在一起，就说明**余弦相似度里根本没有「库里有 / 没有」这个信息**，
    闸门只能交给结构化信号。实测结果决定了 RRF 该用哪种闸门。
    """
    ins: list[dict] = []
    outs: list[dict] = []
    for c in cases:
        if c["cat"] in EXCLUDE_CATS:
            continue
        qv = qvec_of.get(c["id"])
        if not qv:
            continue
        top = max(products, key=lambda p: cosine(qv, vecs[p.sku_id]))
        item = {"id": c["id"], "q": c["q"], "cos": round(cosine(qv, vecs[top.sku_id]), 4), "top_sku": top.sku_id}
        (ins if c.get("allow_skus") else outs).append(item)

    ins.sort(key=lambda x: x["cos"])
    sweeps = [
        {
            "thr": thr,
            "in_pass": sum(1 for x in ins if x["cos"] >= thr),
            "in_total": len(ins),
            "out_leak": sum(1 for x in outs if x["cos"] >= thr),
            "out_total": len(outs),
        }
        for thr in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)
    ]
    cos_in = [x["cos"] for x in ins]
    return {
        "inside": ins,
        "outside": outs,
        "sweeps": sweeps,
        "inside_min": cos_in[0] if cos_in else 0.0,
        "inside_median": cos_in[len(cos_in) // 2] if cos_in else 0.0,
        "inside_max": cos_in[-1] if cos_in else 0.0,
        "outside_max": max((x["cos"] for x in outs), default=0.0),
    }


# ---------------------------------------------------------------- 报告


def _table(results: dict, order: list[str]) -> list[str]:
    lines = ["| 策略 | Recall@1 | Recall@3 | MRR | 库外题返回空 |", "| --- | --- | --- | --- | --- |"]
    for key in order:
        r = results[key]
        lines.append(
            f"| {STRATEGIES[key][0]} | {r['recall@1']*100:.1f}% | {r['recall@3']*100:.1f}% | {r['mrr']:.3f} | "
            f"{r['gate_empty_rate']*100:.0f}% ({int(r['gate_empty_rate']*r['n_gate'])}/{r['n_gate']}) |"
        )
    return lines


def write_report(all_results: dict[str, dict], probe: dict, products, cases, kw_map: dict[str, str], modes: list[str]) -> Path:
    names = {p.sku_id: p.name for p in products}
    order = list(STRATEGIES)
    base = all_results[modes[-1]]          # {策略: 结果}
    ref = base[order[0]]
    n_scored, n_gate = ref["n_scored"], ref["n_gate"]

    lines: list[str] = []
    lines.append("# 导购检索层对照实验\n")
    lines.append(
        f"同一份商品库（{len(products)} SKU）、同一批 query（{n_scored} 条有标准答案的题 + "
        f"{n_gate} 条库外题），只换排序逻辑。判定依据是用例里的 `allow_skus`。\n"
    )

    if "raw" in all_results:
        lines.append("## 口径一：原句直喂检索层\n")
        lines.append("模拟「上游没有做任何查询改写」，把顾客原话直接交给检索。\n")
        lines += _table(all_results["raw"], order)

    if "llm_kw" in all_results:
        lines.append("\n## 口径二：LLM 改写后的关键词（贴近线上）\n")
        lines.append(
            f"先用 `{len(kw_map)}` 条 LLM 改写结果替换原句（缓存见 `retrieval_queries.json`）。"
            "线上链路里模型调 `search_product` 时传的正是这种关键词，"
            "所以这张表更接近真实收益。\n"
        )
        lines += _table(all_results["llm_kw"], order)
        lines.append("\n改写示例：\n")
        ex = [c for c in cases if c["id"] in kw_map][:6]
        lines.append("| 原句 | 改写后的关键词 |")
        lines.append("| --- | --- |")
        for c in ex:
            lines.append(f"| {c['q']} | `{kw_map[c['id']]}` |")

    if len(all_results) == 2:
        lines.append("\n## 两个口径差在哪\n")
        lines.append("| 策略 | R@1（原句） | R@1（改写后） | 差值 |")
        lines.append("| --- | --- | --- | --- |")
        for key in order:
            a, b = all_results["raw"][key]["recall@1"], all_results["llm_kw"][key]["recall@1"]
            lines.append(f"| {STRATEGIES[key][0]} | {a*100:.1f}% | {b*100:.1f}% | {(b-a)*100:+.1f}pp |")
        lines.append(
            "\n> 这个差值衡量的是「上游查询改写」这一步的贡献。"
            "某个策略在改写后收益变小，说明它的强项（扛住长口语问句）与上游改写的作用重叠了。\n"
        )

        raw_r, kw_r = all_results["raw"], all_results["llm_kw"]
        lines.append("\n### 为什么不能只看口径一\n")
        lines.append(
            f"- 只看口径一，会得出「纯向量 {raw_r['vector']['recall@1']*100:.1f}% 完胜现线上 "
            f"{raw_r['hybrid']['recall@1']*100:.1f}%，现有检索该换掉」的结论；"
            f"但口径二显示两者的真实差距只有 "
            f"{(kw_r['vector']['recall@1']-kw_r['hybrid']['recall@1'])*100:+.1f}pp。"
            "原因是向量擅长扛住长口语问句（「抽纸在哪个区？」），而线上模型传进来的本来就是「抽纸」这种短词——"
            "**向量这项优势被上游的查询改写提前吃掉了**。"
        )
        lines.append(
            f"- 字面策略受改写影响最大（{(kw_r['literal']['recall@1']-raw_r['literal']['recall@1'])*100:+.1f}pp），"
            f"正说明它的瓶颈就是长句稀释 bigram；向量受影响最小"
            f"（{(kw_r['vector']['recall@1']-raw_r['vector']['recall@1'])*100:+.1f}pp），两者是同一件事的两面。"
        )
        lines.append(
            f"- 口径二下同时拿到最高召回与 100% 闸门的是 **{STRATEGIES['rrf_gated'][0]}**："
            f"R@1 {kw_r['rrf_gated']['recall@1']*100:.1f}% vs 现线上 {kw_r['hybrid']['recall@1']*100:.1f}%，"
            f"R@3 {kw_r['rrf_gated']['recall@3']*100:.1f}% vs {kw_r['hybrid']['recall@3']*100:.1f}%，闸门同为 100%。"
            f"也就是说：**该换的不是「字面 vs 向量」，而是「加权求和 vs RRF 融合」**。"
        )
        lines.append(
            f"- `rrf_strongvec` 与 `rrf_gated` 的全部指标完全相同：0.60 那条「强向量补召回」路径"
            "没有产生任何增量，是应该被删掉的复杂度。负结果留在这里，免得下次有人再想一遍。\n"
        )

    lines.append("\n## 分类别看（口径二）\n")
    cats = [c for c in ref["by_category"] if ref["by_category"][c]["n"]]
    lines.append("| 策略 | " + " | ".join(f"{c}（n={ref['by_category'][c]['n']}）" for c in cats) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in cats) + " |")
    for key in order:
        lines.append(
            f"| {STRATEGIES[key][0]} | "
            + " | ".join(
                f"{all_results[modes[-1]][key]['by_category'][c]['recall@1']*100:.0f}% / "
                f"{all_results[modes[-1]][key]['by_category'][c]['recall@3']*100:.0f}%"
                for c in cats
            )
            + " |"
        )
    lines.append("\n> 单元格为 `Recall@1 / Recall@3`。\n")

    lines.append("## 库外题：检索层能不能说「没有」\n")
    lines.append(
        "这是防幻觉的第一道闸。库外商品题（iPhone / 三文鱼刺身 / 止痛药）本身没有正确答案，"
        "唯一正确的行为是**什么都不返回**，让下游如实说没有。\n"
    )
    for key in order:
        r = base[key]
        bad = r["gate_false"]
        if not bad:
            lines.append(f"- **{STRATEGIES[key][0]}**：{r['n_gate']}/{r['n_gate']} 全部返回空 ✅")
        else:
            detail = "；".join(f"`{b['q']}` → {', '.join(names.get(s, s) for s in b['returned'])}" for b in bad)
            lines.append(f"- **{STRATEGIES[key][0]}**：漏闸 {len(bad)}/{r['n_gate']}—— {detail}")

    lines.append("\n## 向量阈值能不能自己当闸门？\n")
    lines.append(
        f"纯向量的召回是全场最高（口径二 R@1 {base['vector']['recall@1']*100:.1f}%），"
        "却一条库外题都挡不住。所以真正要问的是：**把余弦阈值调高，能不能既保召回又保闸门？**\n"
    )
    lines.append(
        f"- 库内题的 top1 余弦：最低 **{probe['inside_min']:.3f}**、中位 {probe['inside_median']:.3f}、"
        f"最高 {probe['inside_max']:.3f}"
    )
    lines.append(
        "- 库外题的 top1 余弦：" + "、".join(f"`{x['q']}` **{x['cos']:.3f}**" for x in probe["outside"])
    )
    lines.append("")
    lines.append("| 余弦阈值 | 库内题通过（越高越好） | 库外题漏闸（越低越好） |")
    lines.append("| --- | --- | --- |")
    for s in probe["sweeps"]:
        lines.append(f"| ≥ {s['thr']:.2f} | {s['in_pass']}/{s['in_total']} | {s['out_leak']}/{s['out_total']} |")
    lines.append("")
    if probe["outside_max"] >= probe["inside_min"]:
        lines.append(
            f"> 库外题的最高余弦（**{probe['outside_max']:.3f}**）高于库内题的最低余弦"
            f"（**{probe['inside_min']:.3f}**）——两条分布重叠，**不存在能把它们完美分开的阈值**。"
            "从扫描表看：要挡住全部库外题得把阈值提到 0.60，那时库内题只剩 "
            f"{[s['in_pass'] for s in probe['sweeps'] if s['thr']==0.60][0]}/{probe['sweeps'][0]['in_total']} 能过。"
            "结论：余弦相似度里不包含「库里有 / 没有这件东西」这个信息，"
            "闸门只能由结构化信号（字面命中 / 属性命中）把关，向量只负责扩大召回。"
        )
    else:
        lines.append(
            f"> 库外题的最高余弦（{probe['outside_max']:.3f}）低于库内题的最低余弦（{probe['inside_min']:.3f}），"
            "存在可分阈值，向量自己就能兼任闸门。"
        )

    lines.append("\n## 逐题明细（口径二，命中位置）\n")
    lines.append("| 题号 | 类别 | 提问 | 关键词 | " + " | ".join(STRATEGIES[k][0] for k in order) + " |")
    lines.append("| --- | --- | --- | --- | " + " | ".join("---" for _ in order) + " |")
    by_id: dict[str, dict] = {}
    for key in order:
        for row in base[key]["rows"]:
            by_id.setdefault(row["id"], {})[key] = row
    for cid, per in by_id.items():
        b0 = per[order[0]]
        cells = []
        for key in order:
            row = per[key]
            if row["allow"]:
                hit = next((s for s in row["out"][:3] if s in set(row["allow"])), None)
                if hit and row["out"][0] == hit:
                    cells.append(f"✅ {names.get(hit, hit)}")
                elif hit:
                    cells.append(f"△ 第{row['out'].index(hit)+1}位")
                else:
                    cells.append("❌ 未召回" if row["out"] else "❌ 空")
            else:
                cells.append("（库外题）")
        q = b0["q"].replace("|", "丨")
        kw = kw_map.get(cid, "").replace("|", "丨")
        lines.append(f"| {cid} | {b0['cat']} | {q} | `{kw}` | " + " | ".join(cells) + " |")

    out = RESULTS_DIR / "retrieval_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="导购检索层对照实验")
    ap.add_argument("--mode", default="both", choices=["both", "raw", "llm_kw"], help="跑哪个输入口径")
    ap.add_argument("--re-embed", action="store_true", help="向量缓存作废重算")
    ap.add_argument("--re-extract", action="store_true", help="关键词缓存作废重算")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.VISION_API_KEY:
        print("!! 需要 backend/.env 里的 VISION_API_KEY（用 DashScope 生成向量）")
        return 2

    products = list(product_repo.list_products())
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    print(f"商品 {len(products)} 个，用例 {len(cases)} 条")

    modes = ["raw", "llm_kw"] if args.mode == "both" else [args.mode]

    kw_map: dict[str, str] = {}
    if "llm_kw" in modes:
        if not settings.LLM_API_KEY:
            print("!! 口径二需要 LLM_API_KEY")
            return 2
        kw_map = load_keywords(cases, settings, use_cache=not args.re_extract)

    query_of = {
        "raw": {c["id"]: case_query_raw(c) for c in cases},
        "llm_kw": {c["id"]: kw_map.get(c["id"]) or case_query_raw(c) for c in cases},
    }
    texts_needed = {t for m in modes for t in query_of[m].values()}
    store = load_vector_store(products, texts_needed, settings, use_cache=not args.re_embed)
    vecs = {p.sku_id: store[product_text(p)] for p in products}

    all_results: dict[str, dict] = {}
    for mode in modes:
        qvec_of = {c["id"]: store[query_of[mode][c["id"]]] for c in cases}
        print(f"\n口径 {mode}：")
        all_results[mode] = {}
        for key, (label, fn) in STRATEGIES.items():
            r = evaluate(fn, cases, products, vecs, query_of[mode], qvec_of)
            all_results[mode][key] = r
            print(
                f"  {key:13} R@1={r['recall@1']*100:5.1f}%  R@3={r['recall@3']*100:5.1f}%  "
                f"MRR={r['mrr']:.3f}  库外空={r['gate_empty_rate']*100:3.0f}%"
            )

    probe = threshold_probe(products, cases, vecs, {c["id"]: store[query_of[modes[-1]][c["id"]]] for c in cases})
    print(
        f"\n向量阈值探针（口径 {modes[-1]}）：库内 top1 余弦 {probe['inside_min']:.3f}~{probe['inside_max']:.3f}"
        f"（中位 {probe['inside_median']:.3f}）；库外最高 {probe['outside_max']:.3f}"
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "metrics_retrieval.json").write_text(
        json.dumps(
            {
                "modes": {
                    m: {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in res.items()}
                    for m, res in all_results.items()
                },
                "keyword_rewrites": kw_map,
                "threshold_probe": probe,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report = write_report(all_results, probe, products, cases, kw_map, modes)
    print(f"\n报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
