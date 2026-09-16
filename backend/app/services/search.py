"""混合检索：关键词字面匹配 + 商品属性语义打分。

为什么要做语义这一层：
纯关键词匹配只能命中字面重叠。「红色罐装饮料」跟「可口可乐 汽水」没有一个字相同，
关键词得分必然是 0，顾客就会得到「没找到」——但人一眼就知道那是可乐。

解决办法不是硬上向量模型（要下载、要 Key、演示环境不可控），
而是利用商品库里**已经存在的结构化属性**：颜色、包装形状、品类、口味、健康标签、价格。
这些属性是确定的、可解释的、零依赖的，对「红色罐装饮料」这类描述反而比向量更准。
"""

from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass, field

from app.models.schemas import Product

# ---------------- 属性词典 ----------------

# 颜色名 -> 色相角（0-360）
COLOR_HUES: dict[str, float] = {
    "红": 0, "橙色": 28, "橙": 28, "黄": 52, "绿": 120,
    "青": 175, "蓝": 215, "紫": 280, "粉": 335, "粉红": 335, "棕": 25, "咖啡色": 25,
}

SHAPE_WORDS: dict[str, str] = {
    "罐": "can", "听": "can", "易拉罐": "can",
    "瓶": "bottle", "瓶子": "bottle",
    "盒": "box", "盒装": "box",
    "袋": "bag", "包": "bag", "袋装": "bag",
    "条": "pack", "卷": "pack", "支": "pack",
    "管": "tube", "杯": "cup", "桶": "cup",
}

CATEGORY_WORDS: dict[str, str] = {
    "饮料": "饮料", "喝的": "饮料", "水": "饮料", "饮品": "饮料",
    "零食": "零食", "吃的": "零食", "小吃": "零食", "零嘴": "零食",
    "日用": "日用", "生活用品": "日用", "日化": "日用",
}

HEALTH_TAGS: dict[str, list[str]] = {
    "无糖": ["0糖", "无糖"], "0糖": ["0糖"], "不含糖": ["0糖", "无糖"],
    "低脂": ["0脂"], "0脂": ["0脂"], "零脂": ["0脂"],
    "0卡": ["0卡"], "零卡": ["0卡"], "低卡": ["0卡"],
    "高蛋白": ["高蛋白"], "蛋白质高": ["高蛋白"],
    "低钠": ["低钠"], "咖啡因": ["含咖啡因"], "提神": ["咖啡"],
    "素食": ["素食"], "不辣": [], "辣": ["辣味"],
}

TASTE_WORDS = [
    "原味", "黄瓜", "葡萄", "白桃", "桃子", "薄荷", "麻辣", "香辣",
    "香草", "巧克力", "花生", "坚果", "乌龙", "柠檬", "鸡蛋",
    # 「茶」是品类俗称而非口味，但作用一样：查询里出现「茶」时，
    # 只有名字/标签带茶的商品该被顶上来。缺了它，「既无糖又是茶的饮料」
    # 会因为「茶」不在任何属性词表里而检索不到东方树叶（2026-09-16 评测集暴露）。
    "茶",
]

NEGATIVE_WORDS = ["不含", "不要", "没有", "避免", "过敏"]

# 提问里的疑问词与动词。整句直接参与匹配时，这些字会稀释得分：
# 「可乐在哪里」跟「可口可乐 汽水」只有「可乐」两字重叠，
# 不剥掉「在哪里」，字面分就掉到阈值以下，顾客得到一句「没找到」。
QUESTION_WORDS = [
    "我想问一下", "我想问", "请问", "问一下", "请问一下",
    "在哪里", "在哪儿", "在哪", "哪儿", "哪里", "什么位置", "怎么走",
    "多少钱", "什么价格", "价格是多少", "贵不贵", "几块钱", "几块",
    "有什么", "有哪些", "有哪些", "有啥", "配什么", "搭配什么", "一起买",
    "推荐一下", "推荐", "能吃什么", "可以吃", "能不能吃",
    "营养成分", "营养", "成分", "配料", "热量", "卡路里",
    "促销", "优惠", "打折", "活动",
    "过敏", "不含", "不要",
    # 库存查询专属疑问词：剥掉后「可乐还有货吗」→「可乐」，命中商品名
    "还有货吗", "还有货么", "有货吗", "有货么", "还有没有货", "有没有货",
    "还剩多少", "还剩几", "剩多少", "还剩", "库存多少", "库存还有",
    "买几件", "买多少", "够不够", "售罄", "没货",
    "我想买", "我要买", "买", "拿", "找",
    "的", "了", "吗", "呢", "吧", "啊", "请", "帮我", "谢谢",
]


@dataclass
class QueryAttrs:
    """从自然语言提问中抽出的结构化线索。"""

    colors: list[str] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    health: list[str] = field(default_factory=list)
    tastes: list[str] = field(default_factory=list)
    max_price: float | None = None
    min_price: float | None = None
    exclude_allergens: list[str] = field(default_factory=list)
    negative: bool = False

    @property
    def hit_count(self) -> int:
        return (
            len(self.colors)
            + len(self.shapes)
            + len(self.categories)
            + len(self.health)
            + len(self.tastes)
            + (1 if self.max_price is not None else 0)
            + (1 if self.min_price is not None else 0)
            + len(self.exclude_allergens)
        )


def _hsl(hex_color: str) -> tuple[float, float, float]:
    """返回 (hue 0-360, saturation 0-1, lightness 0-1)。"""
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        return 0.0, 0.0, 1.0
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 0.0, 0.0, 1.0
    hue, lightness, saturation = colorsys.rgb_to_hls(r, g, b)
    return hue * 360, saturation, lightness


def _hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def extract_attributes(query: str) -> QueryAttrs:
    q = (query or "").lower()
    attrs = QueryAttrs()

    for word in COLOR_HUES:
        if word in q:
            attrs.colors.append(word)

    for word, shape in SHAPE_WORDS.items():
        if word in q:
            attrs.shapes.append(shape)

    for word, category in CATEGORY_WORDS.items():
        if word in q:
            attrs.categories.append(category)

    for word, tags in HEALTH_TAGS.items():
        if word in q:
            attrs.health.extend(tags)

    for taste in TASTE_WORDS:
        if taste in q:
            attrs.tastes.append(taste)

    # 价格区间：「30 元以下」「20块以内」「10 到 20 元」
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱|rmb)?\s*(?:以下|以内|之下|不超过)", q)
    if m:
        attrs.max_price = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:以上|往上|超过)", q)
    if m:
        attrs.min_price = float(m.group(1))

    # 过敏原排除：「不含花生」「对花生过敏」
    if any(w in q for w in NEGATIVE_WORDS):
        attrs.negative = True
    for allergen in ["花生", "坚果", "牛奶", "小麦", "大豆", "鸡蛋", "麸质", "乳糖"]:
        if allergen in q and attrs.negative:
            attrs.exclude_allergens.append(allergen)

    # 去重
    attrs.colors = list(dict.fromkeys(attrs.colors))
    attrs.shapes = list(dict.fromkeys(attrs.shapes))
    attrs.categories = list(dict.fromkeys(attrs.categories))
    attrs.health = list(dict.fromkeys(t for t in attrs.health if t))
    attrs.tastes = list(dict.fromkeys(attrs.tastes))
    attrs.exclude_allergens = list(dict.fromkeys(attrs.exclude_allergens))
    return attrs


def _color_score(product: Product, colors: list[str]) -> float:
    if not colors:
        return 0.0
    hue, saturation, lightness = _hsl(product.visual.color)

    best = 0.0
    for name in colors:
        if name in ("白", "黑", "灰"):
            continue
        target = COLOR_HUES.get(name)
        if target is None:
            continue
        # 无彩色（白/灰/黑）无法参与色相匹配，直接判负
        if saturation < 0.15:
            best = max(best, -15.0)
            continue
        distance = _hue_distance(hue, target)
        if distance <= 20:
            best = max(best, 40.0)
        elif distance <= 45:
            best = max(best, 20.0)
        else:
            best = max(best, -12.0)
    return best


def _shape_score(product: Product, shapes: list[str]) -> float:
    if not shapes:
        return 0.0
    return 38.0 if product.visual.shape in shapes else -10.0


def _category_score(product: Product, categories: list[str]) -> float:
    if not categories:
        return 0.0
    return 32.0 if product.category in categories else -8.0


def _health_score(product: Product, health: list[str]) -> float:
    if not health:
        return 0.0
    tags = set(product.tags)
    # 互相包含而非严格相等：顾客说「无糖」，商品标签写的是「无糖茶」，
    # 严格相等会判 0 命中，把唯一一款无糖茶判成「不无糖」。
    hits = sum(1 for h in health if any(h in t or t in h for t in tags))
    if hits:
        return 26.0 * hits
    return -6.0


def _taste_score(product: Product, tastes: list[str]) -> float:
    if not tastes:
        return 0.0
    text = f"{product.name}{product.spec}{''.join(product.tags)}".lower()
    hits = sum(1 for t in tastes if t in text)
    return 24.0 * hits if hits else -4.0


def _price_score(product: Product, attrs: QueryAttrs) -> float:
    score = 0.0
    if attrs.max_price is not None:
        score += 22.0 if product.price <= attrs.max_price else -30.0
    if attrs.min_price is not None:
        score += 22.0 if product.price >= attrs.min_price else -30.0
    return score


def _allergen_score(product: Product, exclude: list[str]) -> float:
    if not exclude:
        return 0.0
    # 排除型需求：命中过敏原是硬否决
    return -100.0 if set(product.allergens) & set(exclude) else 18.0


def semantic_score(product: Product, attrs: QueryAttrs) -> float:
    """属性语义分，可能为负（明确不匹配）。"""
    return (
        _color_score(product, attrs.colors)
        + _shape_score(product, attrs.shapes)
        + _category_score(product, attrs.categories)
        + _health_score(product, attrs.health)
        + _taste_score(product, attrs.tastes)
        + _price_score(product, attrs)
        + _allergen_score(product, attrs.exclude_allergens)
    )


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def keyword_score(product: Product, keyword: str) -> float:
    """字面匹配分：完整命中 > 子串包含 > 二元组重叠。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return 0.0
    name = product.name.lower()
    brand = product.brand.lower()
    spec = product.spec.lower()
    category = product.category.lower()

    score = 0.0
    if kw == name:
        score += 120
    if kw in name:
        score += 60
    if name in kw:
        score += 50
    if kw in brand or brand in kw:
        score += 30
    if kw in spec:
        score += 15
    if kw in category:
        score += 12
    for tag in product.tags:
        if tag.lower() in kw:
            score += 10

    g_kw = _bigrams(kw)
    g_name = _bigrams(name)
    if g_kw:
        score += 25 * len(g_kw & g_name) / len(g_kw)
    return score


def hybrid_score(product: Product, query: str, attrs: QueryAttrs) -> tuple[float, float, float]:
    """返回 (最终分, 关键词分, 语义分)。

    有属性线索时以语义为主——顾客是在描述特征，不是在报商品名；
    没有属性线索时退化为纯字面匹配，保证精确查询仍然稳定。
    """
    kw = min(keyword_score(product, query), 100.0)
    sem = semantic_score(product, attrs) if attrs.hit_count else 0.0

    if attrs.hit_count:
        final = 0.65 * sem + 0.35 * kw
    else:
        final = kw
    return final, kw, sem


def strip_question_words(query: str) -> str:
    """剥掉疑问词，留下真正描述商品的部分。

    清洗后如果什么都不剩（例如整句都是疑问词），就退回原句，
    保证「多少钱」这类纯疑问不会变成空查询。
    """
    text = query or ""
    for word in QUESTION_WORDS:
        text = text.replace(word, " ")
    cleaned = " ".join(text.split())
    return cleaned or (query or "").strip()


def rank_products(products: list[Product], query: str, top_k: int = 3) -> list[Product]:
    attrs = extract_attributes(query)          # 属性从原句提取，避免清洗掉特征词
    core = strip_question_words(query)         # 字面匹配用清洗后的核心词

    ranked: list[tuple[float, str, Product]] = []
    for p in products:
        # 原句与核心词都算一遍取高分：核心词命中率更高，
        # 原句则保留「红色罐装饮料」这类完整描述的语义权重
        score_raw = hybrid_score(p, query, attrs)[0]
        score_core = hybrid_score(p, core, attrs)[0]
        final = max(score_raw, score_core)
        if final >= 8.0:
            ranked.append((final, p.sku_id, p))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    return [p for _, _, p in ranked[:top_k]]
