"""库存功能回归测试。

覆盖四个关键路径：
1. 支付成功后扣减库存（数量正确）
2. 重复支付不重复扣（订单幂等）
3. 库存不足时支付被拦截、不扣减
4. 导购 Agent 能查库存（售罄 / 紧张 / 充足 三态，LLM 工具 + 本地降级）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.database import session_scope
from app.db.models import OrderItemModel, OrderModel, ProductModel
from app.repositories import product_repo
from app.services import agent


def _stock_of(sku_id: str) -> int:
    with session_scope() as db:
        row = db.get(ProductModel, sku_id)
        return row.stock or 0 if row else 0


def _set_stock(sku_id: str, qty: int) -> None:
    with session_scope() as db:
        row = db.get(ProductModel, sku_id)
        if row:
            row.stock = qty


def test_deduct_stock() -> None:
    sku = "SKU001"
    origin = _stock_of(sku)
    try:
        _set_stock(sku, 10)
        problems = product_repo.check_stock({sku: 3})
        assert problems == [], f"库存充足时应无问题，实际 {problems}"

        with session_scope() as db:
            product_repo.deduct_stock(db, {sku: 3})
        assert _stock_of(sku) == 7, "扣减后应为 7"
        print("[PASS] 库存扣减：10 → 7")
    finally:
        _set_stock(sku, origin)


def test_deduct_idempotent() -> None:
    """_persist_order 的幂等：同一 session_id 重复落订单不重复扣库存。"""
    from app.api import checkout
    from app.models.schemas import Bill, CheckoutItem, SessionSnapshot, SessionState

    sku = "SKU002"
    origin = _stock_of(sku)
    try:
        _set_stock(sku, 20)

        sid = f"test-{int(time.time())}"
        item = CheckoutItem(
            sku_id=sku,
            name="测试商品",
            spec="测试规格",
            unit_price=5.0,
            quantity=2,
            subtotal=10.0,
            source="vision",
            confidence=0.9,
        )
        snap = SessionSnapshot(
            session_id=sid,
            state=SessionState.PAID,
            created_at=time.time(),
            paid_method="wechat",
            bill=Bill(
                session_id=sid,
                items=[item],
                origin_amount=10.0,
                discount_amount=0.0,
                payable=10.0,
                total_quantity=2,
            ),
        )

        checkout._persist_order(snap)
        after1 = _stock_of(sku)
        assert after1 == 18, f"首次支付后应为 18，实际 {after1}"

        checkout._persist_order(snap)
        after2 = _stock_of(sku)
        assert after2 == 18, f"重复支付不应再扣，应为 18，实际 {after2}"
        print("[PASS] 支付幂等：重复落订单不重复扣库存")
    finally:
        _set_stock(sku, origin)
        with session_scope() as db:
            db.query(OrderItemModel).filter_by(order_id=sid).delete()
            db.query(OrderModel).filter_by(order_id=sid).delete()


def test_insufficient_stock_blocked() -> None:
    sku = "SKU003"
    origin = _stock_of(sku)
    try:
        _set_stock(sku, 2)
        problems = product_repo.check_stock({sku: 5})
        assert problems, "库存不足时必须有报错"
        assert "库存不足" in problems[0]

        try:
            with session_scope() as db:
                product_repo.deduct_stock(db, {sku: 5})
            raise AssertionError("库存不足时 deduct_stock 必须抛错，不能扣成负数")
        except ValueError as e:
            assert "库存扣减失败" in str(e)
        assert _stock_of(sku) == 2, "扣减失败时库存不应变动"
        print("[PASS] 库存不足：check_stock 报错 + deduct_stock 拒绝执行")
    finally:
        _set_stock(sku, origin)


def test_agent_check_stock_three_states() -> None:
    """LLM 工具路径：check_stock 三态（充足/紧张/售罄）+ citation reason。"""
    sku_ok, sku_low, sku_out = "SKU004", "SKU005", "SKU006"
    saved = {s: _stock_of(s) for s in [sku_ok, sku_low, sku_out]}
    try:
        _set_stock(sku_ok, 50)
        _set_stock(sku_low, 3)
        _set_stock(sku_out, 0)

        text, cites = agent.run_tool(
            "check_stock",
            {"sku_ids": [sku_ok, sku_low, sku_out]},
        )
        assert "充足" in text, f"充足态应出现：{text}"
        assert "仅剩 3 件" in text, f"紧张态应出现：{text}"
        assert "已售罄" in text, f"售罄态应出现：{text}"

        reasons = [c.reason for c in cites]
        assert any("充足" in r for r in reasons), reasons
        assert any("紧张" in r for r in reasons), reasons
        assert any("售罄" in r for r in reasons), reasons
        print("[PASS] LLM 工具 check_stock：充足 / 紧张 / 售罄 三态正确")

        # 本地降级路径：用 detect_intent + _local_answer
        # 用「可乐」这种稳定可检索的关键词，避免依赖 search 命中率
        intent = agent.detect_intent("可乐还有货吗")
        assert intent == "stock", f"应识别为 stock 意图，实际 {intent}"
        local_text, _ = agent._local_answer("可乐还有货吗", "stock")
        assert "库存" in local_text or "售罄" in local_text, local_text
        print("[PASS] 本地降级路径：库存意图识别 + 应答正确")
    finally:
        for s, v in saved.items():
            _set_stock(s, v)


if __name__ == "__main__":
    test_deduct_stock()
    test_deduct_idempotent()
    test_insufficient_stock_blocked()
    test_agent_check_stock_three_states()
    print("\n=== 全部库存测试通过 ===")
