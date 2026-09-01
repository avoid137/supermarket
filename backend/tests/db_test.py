"""数据库层集成测试。

直接验证数据层与服务层，不需要启动 HTTP 服务。
运行：python tests/db_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 直接运行本文件时把 backend/ 加入模块搜索路径，避免必须从项目根用 -m 启动
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.api.checkout import _persist_order
from app.core.config import get_settings
from app.data.products import PRODUCTS
from app.db.database import init_db, session_scope
from app.db.models import OrderItemModel, OrderModel, ProductModel
from app.models.schemas import SessionState
from app.repositories import product_repo
from app.services import vision
from app.services.arbitration import to_bill_lines, weight_check
from app.services.pricing import build_bill
from app.services.store import session_store
from app.services.text2sql import run_readonly_query

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    tag = "OK  " if ok else "FAIL"
    if not ok:
        failed += 1
    print(f"  [{tag}] {name}{(' — ' + detail) if detail else ''}")


def test_seed() -> None:
    print("\n=== 建库与种子数据 ===")
    init_db()
    with session_scope() as db:
        count = db.scalar(select(func.count()).select_from(ProductModel)) or 0
    # 与种子数据比对而不是写死数字，以后增删商品不必改测试
    check("商品入库", count == len(PRODUCTS), f"{count} 个 SKU（种子 {len(PRODUCTS)} 个）")
    check("商品查询接口", product_repo.get_product("SKU001") is not None)


def test_search() -> None:
    print("\n=== 混合检索 ===")
    cases = [
        ("红色罐装饮料", "可口可乐"),
        ("无糖的饮料", "元气森林"),
        ("不含花生的零食", None),
        ("提神的饮料", "三顿半"),
    ]
    for query, expect in cases:
        results = product_repo.search_products(query, top_k=1)
        hit = results[0].name if results else "无结果"
        ok = bool(results) and (expect is None or expect in hit)
        if query == "不含花生的零食" and results:
            ok = "花生" not in "".join(results[0].allergens)
        check(f"「{query}」", ok, hit)


def test_sql_guard() -> None:
    print("\n=== 受控 SQL 防线 ===")
    ok = run_readonly_query("SELECT name, price FROM products WHERE price < 4 LIMIT 5")
    check("正常查询放行", ok["ok"] and ok["row_count"] > 0, f"{ok.get('row_count', 0)} 行")

    for name, sql in [
        ("分号注入", "SELECT name FROM products; DROP TABLE products"),
        ("DELETE 拦截", "DELETE FROM products"),
        ("未知表拦截", "SELECT * FROM users"),
        ("PRAGMA 拦截", "PRAGMA table_info(products)"),
    ]:
        result = run_readonly_query(sql)
        check(name, not result["ok"], result.get("error", "")[:40])

    auto = run_readonly_query("SELECT name FROM products")
    check("自动补 LIMIT", "LIMIT 50" in auto.get("sql", ""), auto.get("sql", "")[-24:])


def test_checkout_flow() -> None:
    print("\n=== 结账链路与订单落库 ===")
    settings = get_settings()

    session = session_store.create()
    detections, mode, elapsed = asyncio.run(
        vision.recognize("S3", None, False, settings)
    )
    check("识别完成", len(detections) == 6, f"{len(detections)} 个目标，{elapsed}ms，模式 {mode}")

    session.detections = detections
    session.tray_weight_g = 865
    session.weight_check = weight_check(detections, 865, settings)
    session.bill = build_bill(
        session.session_id, to_bill_lines(detections, {}), member=True
    )
    session_store.save(session)
    check(
        "遮挡项转人工",
        any(d.state == "review" for d in session.detections),
        session.weight_check["message"] if session.weight_check else "",
    )

    review_box = next(d for d in detections if d.state == "review")
    session.resolved[review_box.box_id] = "SKU017"
    session.bill = build_bill(
        session.session_id, to_bill_lines(detections, session.resolved), member=True
    )
    session.weight_check = weight_check(detections, 865, settings, session.resolved)
    session.state = SessionState.CONFIRMED
    session_store.save(session)
    check("人工补入后称重转正常", session.weight_check["status"] == "ok", session.weight_check["message"])

    reloaded = session_store.get(session.session_id)
    check("会话回读一致", reloaded is not None and len(reloaded.detections) == 6)

    session.state = SessionState.PAID
    session.paid_method = "wechat"
    session_store.save(session)
    _persist_order(session)

    with session_scope() as db:
        order = db.get(OrderModel, session.session_id)
        items = db.scalars(
            select(OrderItemModel).where(OrderItemModel.order_id == session.session_id)
        ).all()
        manual = [i for i in items if i.source == "manual"]

    check("订单入库", order is not None and order.status == "PAID",
          f"应付 ¥{order.payable}" if order else "")
    check("订单明细入库", len(items) == 6, f"{len(items)} 行")
    check("人工项标记正确", len(manual) == 1 and manual[0].sku_id == "SKU017")

    session_store.clear(session.session_id)


def main() -> int:
    test_seed()
    test_search()
    test_sql_guard()
    test_checkout_flow()
    print("\n" + ("全部通过" if failed == 0 else f"存在 {failed} 项失败"))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
