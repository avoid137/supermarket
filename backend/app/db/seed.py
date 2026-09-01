"""把 products.py 中的种子数据写入数据库。

为什么要保留 products.py 而不是直接写 SQL：
它是「人类可读的数据源」，加商品时照着抄一段 Product(...) 最省事。
数据库是运行时的数据源，products.py 是它的初始内容与版本化备份。

用法：
    python -m app.db.seed            # 商品表为空时才导入
    python -m app.db.seed --force    # 清空后重新导入（改了 products.py 之后用）
"""

from __future__ import annotations

from app.data.products import PRODUCTS, SIMILAR_GROUPS, STORE_PROMOTIONS
from app.db.database import session_scope
from app.db.models import ProductModel, PromotionModel, SimilarGroupModel


def build_search_text(product) -> str:
    """拼接检索文本，供关键词检索直接 LIKE 命中。"""
    parts = [
        product.name,
        product.brand,
        product.category,
        product.spec,
        *product.tags,
        product.ingredients,
        product.visual.label,
    ]
    return " ".join(p for p in parts if p).lower()


def seed_products(force: bool = False) -> int:
    with session_scope() as db:
        if force:
            db.query(SimilarGroupModel).delete()
            db.query(PromotionModel).delete()
            db.query(ProductModel).delete()
            db.flush()

        for p in PRODUCTS:
            db.merge(
                ProductModel(
                    sku_id=p.sku_id,
                    name=p.name,
                    brand=p.brand,
                    category=p.category,
                    spec=p.spec,
                    price=p.price,
                    member_price=p.member_price,
                    shelf_aisle=p.shelf.aisle,
                    shelf_level=p.shelf.level,
                    shelf_desc=p.shelf.desc,
                    energy_kj=p.nutrition.energy_kj,
                    protein_g=p.nutrition.protein_g,
                    fat_g=p.nutrition.fat_g,
                    carb_g=p.nutrition.carb_g,
                    sodium_mg=p.nutrition.sodium_mg,
                    ingredients=p.ingredients,
                    allergens=list(p.allergens),
                    tags=list(p.tags),
                    visual_color=p.visual.color,
                    visual_color2=p.visual.color2,
                    visual_shape=p.visual.shape,
                    visual_label=p.visual.label,
                    visual_weight_g=p.visual.weight_g,
                    visual_pkg_width_cm=p.visual.pkg_width_cm,
                    visual_pkg_length_cm=p.visual.pkg_length_cm,
                    stock=p.stock,
                    search_text=build_search_text(p),
                )
            )

        for promo in STORE_PROMOTIONS:
            db.add(
                PromotionModel(
                    scope="store",
                    sku_id=None,
                    type=promo.type,
                    desc=promo.desc,
                    bundle_price=promo.bundle_price,
                    threshold_amount=promo.threshold_amount,
                    discount=promo.discount,
                    groups=promo.groups or None,
                )
            )

        for p in PRODUCTS:
            for promo in p.promotions:
                db.add(
                    PromotionModel(
                        scope="sku",
                        sku_id=p.sku_id,
                        type=promo.type,
                        desc=promo.desc,
                        bundle_price=promo.bundle_price,
                        threshold_amount=promo.threshold_amount,
                        discount=promo.discount,
                        groups=promo.groups or None,
                    )
                )

        for idx, group in enumerate(SIMILAR_GROUPS):
            for sku_id in group:
                db.add(SimilarGroupModel(group_key=f"G{idx + 1}", sku_id=sku_id))

    return len(PRODUCTS)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="导入商品种子数据")
    parser.add_argument("--force", action="store_true", help="清空已有数据后重新导入")
    args = parser.parse_args()

    count = seed_products(force=args.force)
    print(f"已导入 {count} 个 SKU -> {__import__('app.db.database', fromlist=['x']).resolve_database_url()}")
