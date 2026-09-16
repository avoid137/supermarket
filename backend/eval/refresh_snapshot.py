"""从商品主数据重建 eval/catalog_snapshot.json。

评测的真值来源。改过 app/data/products.py（或重置过数据库）之后必须重建一次，
否则用例里的价格 / 货架 / 库存 / 营养期望值会和实际库对不上。

    cd backend && python eval/refresh_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_EVAL_DIR.parent))

from app.repositories import product_repo  # noqa: E402


def build() -> dict:
    products = []
    for p in product_repo.list_products():
        products.append(
            {
                "sku_id": p.sku_id,
                "name": p.name,
                "brand": p.brand,
                "category": p.category,
                "spec": p.spec,
                "price": p.price,
                "member_price": p.member_price,
                "aisle": p.shelf.aisle,
                "level": p.shelf.level,
                "shelf_desc": p.shelf.desc,
                "stock": p.stock,
                "tags": p.tags,
                "allergens": p.allergens,
                "ingredients": p.ingredients,
                "barcode": p.barcode,
                "promotions": [x.desc for x in p.promotions],
                "nutrition": p.nutrition.model_dump(),
                "visual": {
                    "color": p.visual.color,
                    "shape": p.visual.shape,
                    "label": p.visual.label,
                },
            }
        )
    return {
        "generated_from": "backend/app/data/products.py",
        "sku_count": len(products),
        "products": products,
    }


def main() -> int:
    data = build()
    out = _EVAL_DIR / "catalog_snapshot.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stocks = sorted({p["stock"] for p in data["products"]})
    print(f"已写入 {out}\nSKU {data['sku_count']} 个，库存取值 {stocks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
