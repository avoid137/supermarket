"""定价引擎：单品促销 → 组合促销 → 门槛满减，逐层计算并分摊折扣到行项目。"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import Bill, CheckoutItem, Product
from app.repositories import product_repo


@dataclass
class Line:
    product: Product
    quantity: int
    source: str = "auto"       # auto | clarify | manual
    confidence: float = 1.0


def _r2(x: float) -> float:
    return round(x + 1e-9, 2)


def _spread(items: list[CheckoutItem], indices: list[int], total: float) -> None:
    """按数量权重把一笔折扣分摊到若干行，保证分摊总额与 total 完全一致。"""
    if total <= 0 or not indices:
        return
    weights = [items[i].quantity for i in indices]
    total_weight = sum(weights)
    if total_weight == 0:
        return
    allocated = 0.0
    for k, idx in enumerate(indices):
        if k == len(indices) - 1:
            share = _r2(total - allocated)
        else:
            share = _r2(total * weights[k] / total_weight)
            allocated = _r2(allocated + share)
        items[idx].discount = _r2(items[idx].discount + share)
        items[idx].subtotal = _r2(items[idx].subtotal)


def _indices_for(items: list[CheckoutItem], skus: list[str]) -> list[int]:
    return [i for i, it in enumerate(items) if it.sku_id in skus and it.quantity > 0]


def _apply_second_half(items: list[CheckoutItem], lines: list[Line]) -> None:
    for i, line in enumerate(lines):
        if any(p.type == "second_half" for p in line.product.promotions) and line.quantity >= 2:
            pairs = line.quantity // 2
            unit = items[i].unit_price
            discount = _r2(unit * 0.5 * pairs)
            items[i].discount = _r2(items[i].discount + discount)
            items[i].promotions.append(f"第二件半价 -¥{discount}")


def _apply_bundle(items: list[CheckoutItem]) -> None:
    for promo in product_repo.store_promotions():
        if promo.type != "bundle" or len(promo.groups) != 2 or promo.bundle_price is None:
            continue
        idx_a = _indices_for(items, promo.groups[0])
        idx_b = _indices_for(items, promo.groups[1])
        if not idx_a or not idx_b:
            continue
        qty_a = sum(items[i].quantity for i in idx_a)
        qty_b = sum(items[i].quantity for i in idx_b)
        pairs = min(qty_a, qty_b)
        if pairs <= 0:
            continue
        unit_a = items[idx_a[0]].unit_price
        unit_b = items[idx_b[0]].unit_price
        per_pair = _r2(max(0.0, unit_a + unit_b - promo.bundle_price))
        total = _r2(per_pair * pairs)
        if total <= 0:
            continue
        _spread(items, idx_a, _r2(total / 2))
        _spread(items, idx_b, _r2(total - _r2(total / 2)))
        for i in idx_a + idx_b:
            if f"组合价 {promo.desc}" not in items[i].promotions:
                items[i].promotions.append(f"组合价 {promo.desc}")


def _apply_threshold(items: list[CheckoutItem]) -> None:
    for promo in product_repo.store_promotions():
        if promo.type != "threshold" or promo.threshold_amount is None or promo.discount is None:
            continue
        amount_after = sum(_r2(it.subtotal - it.discount) for it in items)
        if amount_after < promo.threshold_amount:
            continue
        # 门槛优惠是整单的，需分摊到行；文案要写分摊额而不是总额，否则顾客误以为每件都减这么多
        before = [it.discount for it in items]
        _spread(items, list(range(len(items))), promo.discount)
        for item, previous in zip(items, before):
            share = _r2(item.discount - previous)
            item.promotions.append(f"{promo.desc}（本件分摊 -¥{share}）")


def build_bill(session_id: str, lines: list[Line], member: bool = False) -> Bill:
    items: list[CheckoutItem] = []
    for line in lines:
        p = line.product
        unit = _r2(p.member_price if member else p.price)
        items.append(
            CheckoutItem(
                sku_id=p.sku_id,
                name=p.name,
                spec=p.spec,
                unit_price=unit,
                quantity=line.quantity,
                subtotal=_r2(unit * line.quantity),
                source=line.source,
                confidence=round(line.confidence, 3),
            )
        )

    _apply_second_half(items, lines)
    _apply_bundle(items)
    _apply_threshold(items)

    origin_amount = _r2(sum(it.subtotal for it in items))
    discount_amount = _r2(sum(it.discount for it in items))
    payable = _r2(max(0.0, origin_amount - discount_amount))

    return Bill(
        session_id=session_id,
        items=items,
        total_quantity=sum(it.quantity for it in items),
        origin_amount=origin_amount,
        discount_amount=discount_amount,
        payable=payable,
    )
