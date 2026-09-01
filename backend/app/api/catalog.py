from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import Product
from app.repositories import product_repo

router = APIRouter()


@router.get("/catalog/products")
async def list_products(keyword: str = "", category: str = "") -> list[Product]:
    if keyword:
        return product_repo.search_products(keyword, top_k=10)
    if category:
        return product_repo.products_by_category(category)
    return product_repo.list_products()


@router.get("/catalog/sku")
async def lookup_by_barcode(barcode: str = Query(..., min_length=4)) -> Product:
    """按条形码查商品。手机扫码后调此接口命中后跳到 /guide?sku=SKUxxx。

    命中失败返回 404，让前端降级为「该条码本店暂未录入」。
    """
    product = product_repo.find_by_barcode(barcode)
    if product is None:
        raise HTTPException(status_code=404, detail=f"未找到条码: {barcode}")
    return product


@router.get("/catalog/products/{sku_id}")
async def get_product(sku_id: str) -> Product:
    product = product_repo.get_product(sku_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"商品不存在: {sku_id}")
    return product


@router.get("/catalog/products/{sku_id}/recommendations")
async def get_recommendations(sku_id: str, top_k: int = 3) -> list[Product]:
    """搭配推荐：扫到商品后，Agent 用它开场给「适合搭配什么」。

    规则：同类商品 + tag 互补 + 同品牌优先；按推荐强度排序。
    """
    if product_repo.get_product(sku_id) is None:
        raise HTTPException(status_code=404, detail=f"商品不存在: {sku_id}")
    return product_repo.recommendations_for(sku_id, top_k=max(1, min(top_k, 6)))


@router.get("/catalog/categories")
async def list_categories() -> list[str]:
    return sorted({p.category for p in product_repo.list_products() if p.category})
