"""新增 4 款商品的识别与检索回归测试。

不依赖网络与真实图片，直接验证：
  1. 闭卷选择清单是否自动包含新增 SKU
  2. S4 场景的三态仲裁结果是否符合预期
  3. 导购混合检索能否命中新商品
  4. 称重校验能否通过
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.data.scenes import SCENE_MAP
from app.repositories import product_repo
from app.services.arbitration import to_bill_lines, weight_check
from app.services.search import rank_products
from app.services.vision import build_catalog, recognize_mock

NEW_SKUS = ["SKU025", "SKU026", "SKU027", "SKU028"]
settings = get_settings()

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")
    if detail and ok:
        print(f"         {detail}")


# ---------- 1. 闭卷选择清单 ----------
print("\n[1] 闭卷选择清单是否包含新增 SKU")
catalog = build_catalog(product_repo.list_products())
for sku in NEW_SKUS:
    product = product_repo.get_product(sku)
    check(f"{sku} 进入清单", sku in catalog, f"{product.name}（{product.spec}）" if product else "商品不存在")

# ---------- 2. S4 场景三态仲裁 ----------
print("\n[2] S4 场景识别结果")
detections, used_scene = recognize_mock("S4", settings)
check("S4 场景被加载", used_scene == "S4", f"实际使用场景 {used_scene}")
check("检测出 4 个目标", len(detections) == 4, f"实际 {len(detections)} 个")

states: dict[str, str] = {}
for det in detections:
    if not det.candidates:
        print(f"         {det.box_id}: 未识别（候选为空）")
        continue
    top = det.candidates[0]
    states[top.sku_id] = det.state
    rivals = ", ".join(f"{c.sku_id}({c.score:.2f})" for c in det.candidates[1:])
    print(f"         {top.sku_id} {top.name}: 置信 {top.score:.3f} -> {det.state}"
          + (f" | 竞争项 {rivals}" if rivals else ""))

check("水溶C100 自动入账", states.get("SKU026") == "auto", f"实际 {states.get('SKU026')}")
check("崂山啤酒 自动入账", states.get("SKU028") == "auto", f"实际 {states.get('SKU028')}")
check("乐事40g 触发追问/复核", states.get("SKU027") in {"clarify", "review"}, f"实际 {states.get('SKU027')}")
check("奥利奥97g 触发追问/复核", states.get("SKU025") in {"clarify", "review"}, f"实际 {states.get('SKU025')}")

# ---------- 3. 称重校验 ----------
print("\n[3] 称重校验")
scene = SCENE_MAP["S4"]
lines = to_bill_lines(detections, None)
check("4 件全部入账（追问项按最高分先行入账）", len(lines) == 4, f"入账 {len(lines)} 行")

expected = sum(
    product_repo.get_product(item.sku_id).visual.weight_g * item.quantity for item in scene.items
)
check("托盘重量与场景标称一致", abs(expected - scene.tray_weight_g) < 0.001,
      f"场景标称 {scene.tray_weight_g}g，按单件重量累加 {expected}g")

wc = weight_check(detections, scene.tray_weight_g, settings)
check("称重校验通过", bool(wc and wc["status"] == "ok"),
      f"实称 {wc['tray_weight_g']:.0f}g / 应重 {wc['expected_weight_g']:.0f}g / 差异 {wc['diff_g']}g")

# 反向验证：顾客把乐事 40g 纠正为 70g，单件重 45g -> 75g，超出 25g 容差，称重必须报警
box_027 = next(d.box_id for d in detections if d.candidates and d.candidates[0].sku_id == "SKU027")
wc_wrong = weight_check(detections, scene.tray_weight_g, settings, resolved={box_027: "SKU010"})
check("纠正为 70g 后称重能发现异常", bool(wc_wrong and wc_wrong["status"] != "ok"),
      f"应重 {wc_wrong['expected_weight_g']:.0f}g vs 实称 {scene.tray_weight_g:.0f}g，"
      f"差异 {wc_wrong['diff_g']}g -> {wc_wrong['status']}")

# ---------- 4. 导购检索 ----------
print("\n[4] 导购检索能否命中新商品")
cases = [
    ("水溶C100", "SKU026"),
    ("崂山啤酒", "SKU028"),
    ("奥利奥饼干", "SKU025"),
    ("乐事薯片", "SKU027"),
]
for query, want in cases:
    hits = rank_products(product_repo.list_products(), query, top_k=3)
    names = [h.name for h in hits]
    check(f"「{query}」命中 {want}", any(h.sku_id == want for h in hits), " -> " + " / ".join(names))

# ---------- 5. 数据完整性 ----------
print("\n[5] 新增商品数据完整性")
for sku in NEW_SKUS:
    p = product_repo.get_product(sku)
    ok = bool(p and p.nutrition and p.shelf and p.visual and p.price > 0)
    check(f"{sku} 字段完整", ok,
          f"{p.name} | ¥{p.price} | {p.spec} | {p.shelf.aisle}-{p.shelf.level}层" if p else "缺失")

print(f"\n{'=' * 46}")
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
