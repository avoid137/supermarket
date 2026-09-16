"""导购 Agent 评测打分器：读 results/raw_<arm>.jsonl，产出指标与明细报告。

只读原始记录、不调模型——所以改判据可以零成本重跑。

---------------- 指标定义（可溯源，别改口径不写注释）----------------

【真值来源】eval/catalog_snapshot.json，由 backend/app/data/products.py 派生。
价格 / 货架 / 库存 / 营养 / 过敏原的期望值一律从这里取，不手抄。

1. 事实正确率 (fact_accuracy)
   该题 facts 全部被回答覆盖的用例占比。fails 明细会列出缺了哪条事实。
   —— 主指标，回答「答得对不对」。

2. 幻觉率 (hallucination_rate)
   回答里出现「数据库不支持的具体店内断言」的用例占比。判据（命中任一即计）：
     a) 命中用例 forbid 列表；
     b) 出现 ¥N / N元 形式的价格，但 N 不等于库里任何 SKU 的售价或会员价
        （排除 省/便宜/差/优惠 等差额语境，排除 N>200 的非单价数字）；
     c) 出现 A1/A2/A3 形式的货架号，但本店不存在该货架；
     d) 该题正确答案是拒答（abstain=true），却仍给出了价格/货架/库存断言。
   —— 核心指标，回答「有没有编」。这是本项目「给大模型划边界」的量化落点。

3. 越界率 (off_target_rate)
   回答提到了 allow_skus 之外的在库商品名。语义是「答非所问 / 顺带推销跑偏」，
   与幻觉分开统计——因为说错和说偏是两类不同的问题。

4. 工具命中率 (tool_hit_rate)
   实际工具调用满足用例 expect_tools 的占比。mode 取 optional 的题不计入分母。
   pure 组不注册工具，指标为 None。

5. 延迟与 tokens：平均毫秒、平均 prompt / completion tokens（用于折算单次成本）。
   单价不写死——不同账号/模型档位不同，待按真实账单填 coefficients。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = _EVAL_DIR / "results"
ARMS = [("pure", "A 纯 LLM 直答"), ("tools", "B 仅工具调用（无约束）"), ("guard", "C 工具+边界约束（现状）")]

# ---------------------------------------------------------------- 数字与文本工具

_NEGATIVE_WORDS = ["没有", "暂无", "不售卖", "未找到", "无法", "抱歉", "不提供", "不能", "不在", "查不到", "不卖", "仅限本店"]


def _num_variants(value: float) -> set[str]:
    raw = f"{value:g}"
    out = {raw}
    if float(value).is_integer():
        out.add(str(int(value)))
        out.add(f"{int(value)}.0")
    return out


def _normalize(text: str) -> str:
    """去掉空白与全角空格，便于中文串匹配（回答里商品名通常不带空格）。"""
    return re.sub(r"[\s\u3000·]+", "", text or "")


_SPEC_FP_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:g|ml|l|kg|粒|枚|只|包|抽|层)", re.IGNORECASE)
_SENT_BOUND = "。！？；!?;\n"


def _spec_fingerprint(spec: str) -> set[str]:
    """规格指纹：把「70g 袋装」压成 {'70g'}，用于同名不同规格的消歧。"""
    return {m.replace(" ", "").lower() for m in _SPEC_FP_RE.findall(_normalize(spec))}


def _sentence_around(text: str, start: int, end: int) -> str:
    """取包含 [start:end] 的整句。折扣/数量语境常写在数字前面十几字之外
    （「买两件只需 ¥5.25」），只看 ±16 字窗口会漏判成凭空报价。"""
    left = 0
    for ch in _SENT_BOUND:
        left = max(left, text.rfind(ch, 0, start) + 1)
    right = len(text)
    for ch in _SENT_BOUND:
        idx = text.find(ch, end)
        if idx != -1:
            right = min(right, idx)
    return text[left:right]


def _has_number(text: str, variants: set[str]) -> bool:
    t = (text or "").replace(",", "")
    for s in variants:
        if re.search(r"(?<![\d.])" + re.escape(s) + r"(?![\d])", t):
            return True
    return False


class Catalog:
    """商品库索引。

    mentions() 刻意做了三层收敛，因为「提到商品」这件事比看上去难：

      1. 名称级 vs 品牌级：只有完整商品名、或**库里唯一**的品牌（如「士力架」「清风」）
         才算提到。乐事 / 元气森林 / 农夫山泉 / 奥利奥 一个品牌对应多个 SKU，
         只出现品牌时无法判断说的是哪一件——硬判会造成大量「越界」误报。
      2. 同名不同规格消歧：库里「乐事原味薯片」有 70g / 40g 两个 SKU，名字完全相同。
         回答里只出现名字时无法判断，此时要求回答同时给出规格（40g / 70g…）
         才认定命中，否则不记——宁可漏判，不可错判。
      3. 否定语境：「避雷：士力架含花生」是在提醒顾客别买，不能算答非所问。
    """

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.products = {p["sku_id"]: p for p in snapshot["products"]}
        self.price_values: set[float] = set()
        self.aisles: set[str] = set()
        names: dict[str, list[str]] = {}
        brands: dict[str, list[str]] = {}
        self.spec_fp: dict[str, set[str]] = {}
        for p in snapshot["products"]:
            self.price_values.update({float(p["price"]), float(p["member_price"])})
            self.aisles.add(p["aisle"])
            names.setdefault(_normalize(p["name"]), []).append(p["sku_id"])
            brands.setdefault(_normalize(p["brand"]), []).append(p["sku_id"])
            self.spec_fp[p["sku_id"]] = _spec_fingerprint(p["spec"])
        self.name_map: dict[str, list[str]] = dict(names)
        for brand, skus in brands.items():
            if len(skus) == 1:  # 品牌唯一时才当商品名用
                self.name_map[brand] = skus

    def mentions(self, text: str) -> set[str]:
        """回答里提到了哪些在库商品。"""
        norm = _normalize(text)
        answer_fp = {m.lower() for m in _SPEC_FP_RE.findall(norm)}
        hit: set[str] = set()
        for needle, skus in self.name_map.items():
            if len(needle) < 2:
                continue
            pos = norm.find(needle)
            if pos < 0:
                continue
            window = norm[max(0, pos - 10) : pos + len(needle) + 10]
            if any(w in window for w in _NEGATION_HINTS):
                continue
            if len(skus) == 1:
                hit.add(skus[0])
            else:
                hit |= {s for s in skus if self.spec_fp[s] & answer_fp}
        return hit


# ---------------------------------------------------------------- 事实解析


def _fact_variants(fact: dict[str, Any], cat: Catalog) -> list[str] | None:
    """把一条事实解析为「可接受的字符串组」。返回 None 表示该事实无法判定、跳过。"""
    t = fact["t"]

    if t == "price":
        p = cat.products[fact["sku"]]
        return sorted(_num_variants(p["price"]) | _num_variants(p["member_price"]))

    if t == "nutri":
        p = cat.products[fact["sku"]]
        return sorted(_num_variants(float(p["nutrition"][fact["field"]])))

    if t == "any_of_nutri":
        out: set[str] = set()
        for sku in fact["skus"]:
            out |= _num_variants(float(cat.products[sku]["nutrition"][fact["field"]]))
        return sorted(out)

    if t == "shelf":
        p = cat.products[fact["sku"]]
        return [p["aisle"], f"{p['level']}层", f"第{p['level']}层"]

    if t == "name":
        p = cat.products[fact["sku"]]
        return [_normalize(p["name"]), _normalize(p["brand"])]

    if t == "stock":
        p = cat.products[fact["sku"]]
        variants = sorted(_num_variants(float(p["stock"])))
        if p["stock"] <= 0:
            variants += ["售罄", "已售完", "缺货", "没有库存"]
        elif p["stock"] <= 5:
            variants += ["仅剩", "库存紧张", "紧张"]
        else:
            variants += ["充足", "有货", "库存足够", "还有"]
        return variants

    if t == "allergen":
        p = cat.products[fact["sku"]]
        target = fact["name"]
        return [target] if target in p["allergens"] else ["__不可达__"]

    if t == "ingredient_kw":
        return [fact["kw"]]

    if t == "promo":
        p = cat.products[fact["sku"]]
        return [d for d in p["promotions"]] or ["__无促销__"]

    if t == "any_of_skus":
        out2: list[str] = []
        for sku in fact["skus"]:
            out2 += [_normalize(cat.products[sku]["name"]), _normalize(cat.products[sku]["brand"])]
        return out2

    if t == "negative":
        return _NEGATIVE_WORDS

    if t == "regex":
        return None  # 单独处理

    raise ValueError(f"unknown fact type: {t}")


def _fact_satisfied(fact: dict[str, Any], answer: str, cat: Catalog) -> tuple[bool, str]:
    if fact["t"] == "regex":
        ok = re.search(fact["pattern"], answer or "") is not None
        return ok, f"regex({fact['pattern']})"

    if fact["t"] in ("nutri", "any_of_nutri"):
        variants = _fact_variants(fact, cat) or []
        return _has_number(answer, set(variants)), f"{fact['t']}={fact.get('field')}∈{variants}"

    variants = _fact_variants(fact, cat)
    if variants is None:
        return True, "skipped"
    norm = _normalize(answer)
    if fact["t"] in ("price", "any_of_skus", "allergen", "ingredient_kw", "promo"):
        for v in variants:
            if _normalize(v) in norm:
                return True, f"{fact['t']}缺:{variants[:4]}"
        return False, f"{fact['t']}缺:{variants[:4]}"
    # shelf / name / stock 走同一套：命中任一可接受串即可
    for v in variants:
        if _normalize(v) in norm:
            return True, f"{fact['t']}缺:{variants[:4]}"
    return False, f"{fact['t']}缺:{variants[:4]}"


# ---------------------------------------------------------------- 幻觉检测

_PRICE_RE = re.compile(r"(?:¥|￥)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*元(?![以之])")
_AISLE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]\d)(?![0-9])")
# 「库存 100 件」「还剩 97」「剩余 12 瓶」这类带数字的库存断言
_STOCK_RE = re.compile(r"(?:库存|还剩|剩余|剩下)\s*(?:约)?\s*(\d+)")

# 差额/折扣语境：出现这些词时，数字是「便宜了多少」而不是单品售价
_DIFF_WORDS = ("省", "便宜", "差", "降", "优惠", "立减", "低于", "贵")
# 数量/合计语境：出现这些词时，数字是算出来的总价（如「5 包 = ¥27.5」「第二件半价 → 两瓶 ¥3」），
# 不等于货架单价，不该算凭空报价。不加这条例会把正确的算术判成幻觉。
_DERIVED_HINTS = (
    "第二件", "半价", "买两", "买二", "买三", "买5", "买五", "两瓶", "两件", "两盒", "两包",
    "5包", "五包", "三瓶", "总共", "一共", "合计", "总计", "总价", "加起来", "=", "×", "*",
)
# 否定/排除语境：出现这些词时命中 forbid 是在提醒顾客「避开」，属于正确行为
# 否定语境词表。刻意不含单字「无」——它会在「炫迈无糖口香糖」这类商品名里误命中，
# 把正常提及也一起压掉。宁可少拦，不要误拦。
_NEGATION_HINTS = (
    "避开", "避雷", "避免", "不要", "别选", "别买", "排除", "不建议", "不推荐", "远离",
    "不含", "没有", "未售", "不售",
)


def _question_numbers(q: str) -> set[float]:
    """问题里出现过的数字。顾客自己给的预算（「10 块钱以内」）不该被算成凭空报价。"""
    out: set[float] = set()
    for raw in re.findall(r"\d+(?:\.\d+)?", q or ""):
        try:
            out.add(float(raw))
        except ValueError:
            continue
    return out


def _detect_hallucination(case: dict[str, Any], answer: str, cat: Catalog) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    norm = _normalize(answer)
    q_values = _question_numbers(case.get("q", ""))

    for bad in case.get("forbid") or []:
        for m in re.finditer(re.escape(_normalize(bad)), norm):
            near = norm[max(0, m.start() - 8) : m.end() + 8]
            sentence = _sentence_around(norm, m.start(), m.end())
            if any(w in near for w in _NEGATION_HINTS) or any(w in sentence for w in _NEGATION_HINTS):
                continue  # 「避雷：士力架含花生」是正确提醒，不是误荐
            flags.append({"kind": "forbidden", "evidence": bad})
            break

    for m in _PRICE_RE.finditer(answer or ""):
        raw = m.group(1) or m.group(2)
        try:
            val = float(raw)
        except ValueError:
            continue
        if val > 200 or val in q_values:
            continue
        window = (answer or "")[max(0, m.start() - 16) : m.end() + 8]
        sentence = _sentence_around(answer or "", m.start(), m.end())
        if any(w in window for w in _DIFF_WORDS) or any(w in sentence for w in _DERIVED_HINTS):
            continue
        if not any(abs(val - p) < 1e-6 for p in cat.price_values):
            flags.append({"kind": "price_not_in_catalog", "evidence": window.strip()})

    for m in _AISLE_RE.finditer(answer or ""):
        if m.group(1) not in cat.aisles:
            flags.append(
                {
                    "kind": "shelf_not_in_catalog",
                    "evidence": (answer or "")[max(0, m.start() - 10) : m.end() + 10].strip(),
                }
            )

    # 库存数：prompt 同样禁止凭记忆编造库存。断言了具体件数但与本题允许商品的
    # 真实库存都不符 → 凭空库存。（「库存充足」这类不给数字的定性回答不判。）
    for m in _STOCK_RE.finditer(answer or ""):
        try:
            val = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if val in q_values:
            continue
        allow_stock = {
            float(cat.products[s]["stock"]) for s in (case.get("allow_skus") or []) if s in cat.products
        }
        if allow_stock and val not in allow_stock:
            window = (answer or "")[max(0, m.start() - 12) : m.end() + 8]
            flags.append({"kind": "stock_not_in_catalog", "evidence": window.strip()})

    if case.get("abstain"):
        has_assertion = bool(list(_PRICE_RE.finditer(answer or ""))) or bool(
            list(_AISLE_RE.finditer(answer or ""))
        ) or bool(re.search(r"(库存|还剩|有货|剩余)\s*[0-9]", answer or ""))
        if has_assertion:
            flags.append({"kind": "abstain_violation", "evidence": (answer or "")[:80]})

    return flags


# ---------------------------------------------------------------- 库外商品推荐
#
# 上面那套判据只抓「数值/位置类」编造。还有一类幻觉它抓不到：**顺口推荐店里没有的商品**
# （模型凭常识说「选无糖就喝绿茶」「可以买饭团」），违反 prompt「工具没查到的商品直接说没有」。
# 这类错误没有可校验的数值，只能靠词表启发式识别，所以**单独统计、不并入幻觉率**。
#
# 词表是「常见零售商品词」而非针对某次回答定制；且与库内任何商品名/品牌/规格重合的词
# 会在加载时自动剔除（避免把自家商品算成库外货）。
_OUT_OF_CATALOG_WORDS = [
    # 饮料
    "雪碧", "芬达", "美年达", "七喜", "冰红茶", "绿茶", "茉莉花茶", "茉莉蜜茶", "巴黎水",
    "东鹏", "尖叫", "红牛", "脉动", "佳得乐", "维他柠檬茶", "阿萨姆", "奶茶", "拿铁", "美式",
    "星巴克", "瑞幸", "养乐多", "乳酸菌", "酸奶", "电解质水", "纯净水", "宝矿力", "王老吉",
    "加多宝", "三得利", "伊藤园",
    # 零食
    "旺仔", "魔芋爽", "小馒头", "薯条", "泡面", "方便面", "面包", "蛋糕", "巧克力豆",
    "彩虹糖", "费列罗", "好时", "乐天",
    # 速食与日用
    "饭团", "关东煮", "便当", "寿司", "三明治", "洗发水", "沐浴露", "洗面奶", "洗手液", "湿巾",
    "卫生纸", "洗衣粉", "防晒霜",
]
_OUT_CONTEXT_EXCLUDE = (
    "没有", "暂无", "不卖", "不售卖", "抱歉", "未找到", "查不到", "别选", "不建议", "不是", "非",
    # 「要我帮你找找寿司吗」是提出去查、不是断言店里有，不算推荐库外商品
    "帮你", "要我帮", "需要我帮", "你说一声", "有的话", "有没有", "找找",
)


_DSML_LEAK_RE = re.compile(r"<[|｜]|DSML")


def dsml_leaked(answer: str) -> bool:
    """回答正文里是否漏出模型私有协议标记（<|DSML|tool_calls|> 这类）。

    这类内容本该只出现在 tool_calls 字段，漏进正文会直接显示给顾客。
    C 组比 B 组多一条「第二轮禁止再调工具、禁止出现协议标记」的硬约束，
    这个指标就是那条约束的量化价值。
    """
    return bool(_DSML_LEAK_RE.search(answer or ""))


def _out_of_catalog_hits(answer: str, cat: Catalog) -> list[str]:
    """回答里提到的库外商品词（否定语境内的不算）。"""
    norm = _normalize(answer)
    catalog_blob = _normalize(
        "".join(f"{p['name']}{p['brand']}{p['spec']}" for p in cat.products.values())
    )
    hits: list[str] = []
    for word in _OUT_OF_CATALOG_WORDS:
        if _normalize(word) in catalog_blob:
            continue  # 自家就有的词，不算库外
        for m in re.finditer(re.escape(word), norm):
            window = norm[max(0, m.start() - 12) : m.end() + 12]
            if any(w in window for w in _OUT_CONTEXT_EXCLUDE):
                continue
            hits.append(word)
            break
    return hits


# ---------------------------------------------------------------- 主流程


def score_arm(arm: str, path: Path, cases: dict[str, dict], cat: Catalog) -> dict[str, Any]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    n = len(records)
    ok_fact = 0
    ok_tool = 0
    tool_denom = 0
    off_target = 0
    errors = 0
    tot_lat = tot_pt = tot_ct = 0
    hal_records = 0
    ooc_records = 0
    leak_records = 0
    details: list[dict[str, Any]] = []
    hal_detail: list[dict[str, Any]] = []
    ooc_detail: list[dict[str, Any]] = []
    leak_detail: list[dict[str, Any]] = []
    cat_stat: dict[str, list[int]] = {}

    for r in records:
        case = cases[r["id"]]
        answer = r["answer"] or ""
        if r["error"]:
            errors += 1

        fact_results = []
        all_ok = True
        for fact in case["facts"]:
            ok, why = _fact_satisfied(fact, answer, cat)
            fact_results.append({"ok": ok, "why": why})
            all_ok = all_ok and ok
        ok_fact += int(all_ok)

        mentions = cat.mentions(answer)
        allow = set(case.get("allow_skus") or [])
        # open_scope 的题（开放推荐 / 咨询）本就允许顺手推荐别的商品，
        # 不参与越界判定，否则会把「多推荐一款」误判成「答非所问」。
        out_of_scope = set() if case.get("open_scope") else (mentions - allow)
        if out_of_scope:
            off_target += 1

        hal = _detect_hallucination(case, answer, cat)
        if hal:
            hal_records += 1
            hal_detail.append({"id": r["id"], "q": r["question"], "answer": answer[:220], "flags": hal})

        if dsml_leaked(answer):
            leak_records += 1
            leak_detail.append({"id": r["id"], "q": r["question"], "answer": answer[:220]})

        ooc = _out_of_catalog_hits(answer, cat)
        if ooc:
            ooc_records += 1
            ooc_detail.append({"id": r["id"], "q": r["question"], "words": ooc, "answer": answer[:220]})

        mode = case["expect_tools"]["mode"]
        names = case["expect_tools"]["names"]
        tool_ok: bool | None = None
        if arm != "pure" and mode != "optional":
            tool_denom += 1
            actual = set(r["tool_names"])
            if mode == "all_of":
                tool_ok = set(names) <= actual
            elif mode == "any_of":
                tool_ok = bool(set(names) & actual)
            elif mode == "none":
                tool_ok = not actual
            ok_tool += int(bool(tool_ok))

        tot_lat += r["latency_ms"]
        tot_pt += r["prompt_tokens"]
        tot_ct += r["completion_tokens"]

        stat = cat_stat.setdefault(r["cat"], [0, 0, 0])
        stat[0] += int(all_ok)
        stat[1] += 1
        stat[2] += int(bool(hal))

        if not all_ok or out_of_scope or hal:
            details.append(
                {
                    "id": r["id"],
                    "cat": r["cat"],
                    "q": r["question"],
                    "answer": answer[:240],
                    "tools": r["tool_names"],
                    "fact_results": fact_results,
                    "out_of_scope": sorted(out_of_scope),
                    "hall": hal,
                }
            )

    return {
        "arm": arm,
        "n": n,
        "errors": errors,
        "fact_accuracy": round(ok_fact / n, 4) if n else None,
        "hallucination_rate": round(hal_records / n, 4) if n else None,
        "out_of_catalog_rate": round(ooc_records / n, 4) if n else None,
        "dsml_leak_rate": round(leak_records / n, 4) if n else None,
        "off_target_rate": round(off_target / n, 4) if n else None,
        "tool_hit_rate": round(ok_tool / tool_denom, 4) if tool_denom else None,
        "avg_latency_ms": int(tot_lat / n) if n else 0,
        "avg_prompt_tokens": int(tot_pt / n) if n else 0,
        "avg_completion_tokens": int(tot_ct / n) if n else 0,
        "by_category": {
            k: {
                "fact_accuracy": round(v[0] / v[1], 3),
                "n": v[1],
                "hallucination_rate": round(v[2] / v[1], 3),
            }
            for k, v in sorted(cat_stat.items())
        },
        "details": details,
        "hall_detail": hal_detail,
        "ooc_detail": ooc_detail,
        "leak_detail": leak_detail,
    }


def _flat(text: str) -> str:
    """压掉换行，避免把 markdown 表格撑破。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="导购 Agent 评测打分")
    parser.add_argument("--quiet", action="store_true", help="只输出总表")
    args = parser.parse_args()

    snapshot = json.loads((_EVAL_DIR / "catalog_snapshot.json").read_text(encoding="utf-8"))
    cat = Catalog(snapshot)
    cases = {
        c["id"]: c
        for c in json.loads((_EVAL_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]
    }

    arms = [(a, label) for a, label in ARMS if (RESULTS_DIR / f"raw_{a}.jsonl").exists()]
    if not arms:
        print("没有找到任何 raw_*.jsonl，请先跑 run_eval.py", file=sys.stderr)
        return 2

    results = {a: score_arm(a, RESULTS_DIR / f"raw_{a}.jsonl", cases, cat) for a, _ in arms}

    metrics_path = RESULTS_DIR / "metrics.json"
    metrics_path.write_text(
        json.dumps(
            {
                k: {kk: vv for kk, vv in v.items() if kk not in ("details", "hall_detail", "ooc_detail", "leak_detail")}
                for k, v in results.items()
            },
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append("# 导购 Agent 三方案评测报告\n")
    lines.append(f"> 用例数 {results[arms[0][0]]['n']} ｜ 真值来源 `eval/catalog_snapshot.json`（由 `products.py` 派生）")
    lines.append("> 判据定义见 `eval/score_eval.py` 文件头注释\n")
    lines.append("## 一、总表\n")
    lines.append(
        "| 配置 | 事实正确率 | 幻觉率 | 库外商品率 | 越界率 | 协议标记泄漏 | 工具命中率 | 平均延迟 | 平均 prompt tok | 平均 completion tok |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for a, label in arms:
        r = results[a]
        lines.append(
            f"| {label} | **{_pct(r['fact_accuracy'])}** | **{_pct(r['hallucination_rate'])}** | "
            f"**{_pct(r['out_of_catalog_rate'])}** | "
            f"{_pct(r['off_target_rate'])} | **{_pct(r['dsml_leak_rate'])}** | "
            f"{_pct(r['tool_hit_rate'])} | {r['avg_latency_ms']} ms | "
            f"{r['avg_prompt_tokens']} | {r['avg_completion_tokens']} |"
        )

    lines.append("\n## 二、分类别再分（事实正确率 / 幻觉率）\n")
    cats = sorted({c for a, _ in arms for c in results[a]["by_category"]})
    header = "| 类别 | " + " | ".join(label for _, label in arms) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(arms) + 1))
    for c in cats:
        cells = []
        for a, _ in arms:
            d = results[a]["by_category"].get(c)
            cells.append("—" if not d else f"{d['fact_accuracy'] * 100:.0f}% / {d['hallucination_rate'] * 100:.0f}% (n={d['n']})")
        lines.append(f"| {c} | " + " | ".join(cells) + " |")

    for a, label in arms:
        r = results[a]
        lines.append(f"\n## 三、{label} · 问题用例明细\n")
        if r["hall_detail"]:
            lines.append("### 幻觉明细（需人工复核判据是否误报）\n")
            lines.append("| 用例 | 问题 | 命中判据 | 证据片段 |")
            lines.append("|---|---|---|---|")
            for h in r["hall_detail"]:
                kinds = "、".join(sorted({f["kind"] for f in h["flags"]}))
                ev = " ／ ".join(_flat(f["evidence"])[:40] for f in h["flags"][:2])
                lines.append(f"| {h['id']} | {h['q'][:22]} | {kinds} | {ev} |")
        if r["ooc_detail"]:
            lines.append("\n### 推荐了库内没有的商品（词表启发式，需人工复核）\n")
            lines.append("| 用例 | 问题 | 命中词 | 回答摘录 |")
            lines.append("|---|---|---|---|")
            for h in r["ooc_detail"]:
                lines.append(
                    f"| {h['id']} | {h['q'][:20]} | {'、'.join(h['words'])} | "
                    f"{_flat(h['answer'])[:60]} |"
                )
        notok = [d for d in r["details"] if not all(f["ok"] for f in d["fact_results"])]
        if notok:
            lines.append("\n### 事实未覆盖\n")
            lines.append("| 用例 | 类别 | 问题 | 缺哪条事实 | 回答摘录 |")
            lines.append("|---|---|---|---|---|")
            for d in notok:
                missing = "；".join(f["why"] for f in d["fact_results"] if not f["ok"])
                lines.append(
                    f"| {d['id']} | {d['cat']} | {d['q'][:20]} | {_flat(missing)[:60]} | {_flat(d['answer'])[:70]} |"
                )

    report = "\n".join(lines) + "\n"
    (RESULTS_DIR / "report.md").write_text(report, encoding="utf-8")

    if not args.quiet:
        print(report)
    else:
        for a, label in arms:
            r = results[a]
            print(
                f"{label}: 事实 {_pct(r['fact_accuracy'])} 幻觉 {_pct(r['hallucination_rate'])} "
                f"库外商品 {_pct(r['out_of_catalog_rate'])} 越界 {_pct(r['off_target_rate'])} "
                f"协议泄漏 {_pct(r['dsml_leak_rate'])} 工具 {_pct(r['tool_hit_rate'])}"
            )
    print(f"\n→ {RESULTS_DIR / 'report.md'}\n→ {metrics_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
