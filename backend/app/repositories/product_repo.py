"""商品仓储：对外返回 Pydantic 的 Product，数据库细节不外泄。

服务层只认 Product，不认 ORM——这样以后换数据库或加缓存，
改动都被关在这一层里。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import session_scope
from app.db.models import (
    ProductModel,
    PromotionModel,
    SimilarGroupModel,
)
from app.models.schemas import (
    Nutrition,
    Product,
    Promotion,
    ShelfLocation,
    VisualProfile,
)
from app.services.search import rank_products


def _to_promotion(model: PromotionModel) -> Promotion:
    return Promotion(
        type=model.type,
        desc=model.desc,
        with_skus=[],
        groups=model.groups or [],
        bundle_price=model.bundle_price,
        threshold_amount=model.threshold_amount,
        discount=model.discount,
    )


def _to_product(model: ProductModel, promotions: list[Promotion] | None = None) -> Product:
    return Product(
        sku_id=model.sku_id,
        name=model.name,
        brand=model.brand or "",
        category=model.category or "",
        spec=model.spec or "",
        price=model.price,
        member_price=model.member_price,
        shelf=ShelfLocation(
            aisle=model.shelf_aisle or "",
            level=model.shelf_level or 1,
            desc=model.shelf_desc or "",
        ),
        nutrition=Nutrition(
            energy_kj=model.energy_kj or 0,
            protein_g=model.protein_g or 0,
            fat_g=model.fat_g or 0,
            carb_g=model.carb_g or 0,
            sodium_mg=model.sodium_mg or 0,
        ),
        ingredients=model.ingredients or "",
        allergens=list(model.allergens or []),
        tags=list(model.tags or []),
        promotions=promotions or [],
        visual=VisualProfile(
            color=model.visual_color or "#CCCCCC",
            color2=model.visual_color2 or "#FFFFFF",
            shape=model.visual_shape or "box",
            label=model.visual_label or "",
            weight_g=model.visual_weight_g or 0,
            pkg_width_cm=model.visual_pkg_width_cm or 0.0,
            pkg_length_cm=model.visual_pkg_length_cm or 0.0,
        ),
        stock=model.stock or 0,
        barcode=getattr(model, "barcode", None),
    )


def _load_promotion_map() -> dict[str, list[Promotion]]:
    with session_scope() as db:
        rows = db.scalars(
            select(PromotionModel).where(PromotionModel.sku_id.is_not(None))
        ).all()
    mapping: dict[str, list[Promotion]] = {}
    for row in rows:
        mapping.setdefault(row.sku_id, []).append(_to_promotion(row))
    return mapping


def list_products() -> list[Product]:
    with session_scope() as db:
        rows = db.scalars(select(ProductModel).order_by(ProductModel.sku_id)).all()
    promo_map = _load_promotion_map()
    return [_to_product(r, promo_map.get(r.sku_id, [])) for r in rows]


def get_product(sku_id: str) -> Product | None:
    with session_scope() as db:
        row = db.get(ProductModel, sku_id)
        if row is None:
            return None
        promos = db.scalars(
            select(PromotionModel).where(PromotionModel.sku_id == sku_id)
        ).all()
    return _to_product(row, [_to_promotion(p) for p in promos])


def find_by_barcode(barcode: str) -> Product | None:
    """按条形码精确查商品。手机扫码后调此接口跳转。

    演示用：barcode 字段 nullable；未贴条码的 SKU 走 keyword 模糊搜仍能命中。
    """
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    with session_scope() as db:
        row = db.scalars(
            select(ProductModel).where(ProductModel.barcode == barcode)
        ).first()
        if row is None:
            return None
        promos = db.scalars(
            select(PromotionModel).where(PromotionModel.sku_id == row.sku_id)
        ).all()
    return _to_product(row, [_to_promotion(p) for p in promos])


def recommendations_for(sku_id: str, top_k: int = 3) -> list[Product]:
    """为某个 SKU 推荐搭配商品。规则：

    1. 优先从「同类的非自身」里挑（饮料配饮料、零食配零食）。
    2. 优先选择 tag 中含「解渴 / 提神 / 佐餐 / 甜食 / 咸食」互补的商品。
    3. 库存为 0 的剔除。

    返回顺序按推荐强度递减，最多 top_k 个。
    """
    target = get_product(sku_id)
    if target is None:
        return []

    # 简单的「互补标签」映射：根据常识搭，不依赖外部数据
    complementary_tags: dict[str, list[str]] = {
        "薯片": ["解渴", "提神", "佐餐"],
        "可乐": ["解渴", "提神", "佐餐"],
        "饼干": ["解渴", "佐餐"],
        "巧克力": ["解渴", "提神"],
        "坚果": ["佐餐", "解渴"],
        "口香糖": ["提神", "清新"],
        "牛奶": ["提神", "早餐"],
        "咖啡": ["甜食", "佐餐"],
        "茶": ["甜食", "佐餐"],
        "啤酒": ["咸食", "佐餐"],
        "水": ["提神", "佐餐"],
        "气泡水": ["咸食", "佐餐"],
    }
    target_keywords = []
    for kw, tags in complementary_tags.items():
        if kw in target.name or kw in target.brand:
            target_keywords = tags
            break

    candidates = [
        p
        for p in list_products()
        if p.sku_id != target.sku_id and p.category == target.category and p.stock > 0
    ]
    # 评分：tag 命中数优先，再看是否同品牌（同品牌的近邻适合搭配）
    def score(p: Product) -> tuple[int, int, int]:
        overlap = len(set(p.tags) & set(target_keywords))
        same_brand = 1 if (p.brand and p.brand == target.brand) else 0
        return (-overlap, -same_brand, -p.stock)

    candidates.sort(key=score)
    return candidates[:top_k]


def list_products_by_ids(sku_ids: list[str]) -> list[Product]:
    """按 sku_id 列表批量精确取货，保持入参顺序。

    check_stock 工具用：顾客问「这几件还有货吗」时，必须按 id 精确取，
    不能走模糊搜索——模糊搜会命中同名不同规格的多个 SKU。
    """
    if not sku_ids:
        return []
    with session_scope() as db:
        rows = db.scalars(
            select(ProductModel).where(ProductModel.sku_id.in_(sku_ids))
        ).all()
        promo_rows = db.scalars(
            select(PromotionModel).where(PromotionModel.sku_id.in_(sku_ids))
        ).all()
    promo_map: dict[str, list[Promotion]] = {}
    for p in promo_rows:
        promo_map.setdefault(p.sku_id, []).append(_to_promotion(p))
    # 保持入参顺序，让回答里商品排列可预期
    by_id = {r.sku_id: r for r in rows}
    return [_to_product(by_id[s], promo_map.get(s, [])) for s in sku_ids if s in by_id]


def search_products(query: str, top_k: int = 3) -> list[Product]:
    """混合检索：先用 SQL 粗筛，再用属性语义精排。"""
    return rank_products(list_products(), query, top_k=top_k)


def products_by_category(category: str) -> list[Product]:
    return [p for p in list_products() if p.category == category]


def products_with_promotion() -> list[Product]:
    return [p for p in list_products() if p.promotions]


def filter_excluding_allergens(allergens: list[str]) -> list[Product]:
    """找出不含指定过敏原的商品，用于回答「我对花生过敏能吃什么」。"""
    exclude = {a for a in allergens if a}
    if not exclude:
        return []
    return [p for p in list_products() if not (set(p.allergens) & exclude)]


def store_promotions() -> list[Promotion]:
    with session_scope() as db:
        rows = db.scalars(
            select(PromotionModel).where(PromotionModel.scope == "store")
        ).all()
    return [_to_promotion(r) for r in rows]


def similar_groups() -> list[list[str]]:
    with session_scope() as db:
        rows = db.scalars(select(SimilarGroupModel)).all()
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row.group_key, []).append(row.sku_id)
    return [sorted(v) for v in groups.values()]


# ---------------------------------------------------------------- 库存操作


def check_stock(items: dict[str, int]) -> list[str]:
    """校验库存是否足够，返回不足的提示列表（空列表 = 全部够用）。

    在支付前调用，把「库存不足」拦在扣款之前——顾客不应为拿不到的货付钱。
    """
    problems: list[str] = []
    with session_scope() as db:
        for sku_id, qty in items.items():
            row = db.get(ProductModel, sku_id)
            if row is None:
                problems.append(f"{sku_id} 不存在")
            elif (row.stock or 0) < qty:
                name = row.name or sku_id
                problems.append(f"{name} 库存不足：仅剩 {row.stock or 0} 件，需要 {qty} 件")
    return problems


def deduct_stock(db: Session, items: dict[str, int]) -> None:
    """按 sku->数量 扣减库存。必须与订单写入共用同一事务，由调用方传入 Session。

    幂等性由调用方保证（只在新订单首次落库时调用）；
    库存扣到负数是数据事故，这里直接抛错让整个事务回滚。
    """
    for sku_id, qty in items.items():
        row = db.get(ProductModel, sku_id)
        if row is None:
            raise ValueError(f"库存扣减失败：商品不存在 {sku_id}")
        if (row.stock or 0) < qty:
            raise ValueError(
                f"库存扣减失败：{row.name or sku_id} 仅剩 {row.stock or 0} 件，需要 {qty} 件"
            )
        row.stock = (row.stock or 0) - qty
