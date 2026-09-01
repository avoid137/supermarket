"""相似组重量终裁（模拟称重传感器）回归测试。

背景：40g/70g 这类外观一致的商品，视觉模型常分不开。系统已有 tray_weight_g
（整盘总重）字段，但原先只用于识别后的「称重校验」。本测试验证把整盘总重
前移到「识别解析」阶段，作为 OCR/尺子/尺寸排序都失败时的**最后终裁**信号：

- 用「整盘总重 − 皮重 − 已确认实例重量」的残差，枚举相似组歧义实例的
  规格组合，唯一落在容差内的组合被采纳（_reassign_detection_sku 真正切换 SKU）。
- OCR 已确认的实例视为 definite，重量计入基准、不参与重排、绝不被覆盖。
- tray_weight_g 为 None 时跳过；托盘上存在无法归类的 review 项时跳过（宁可不自动入账）。

覆盖场景：
1. 两袋乐事 + 总重 120g → 一 70g 一 40g（组合唯一确定）
2. 单袋歧义 + 残差 45g → 40g；残差 75g → 70g
3. OCR 已确认 70g 不被重量覆盖（重量仅作最后终裁）
4. tray_weight_g=None → 跳过，保持 review
5. 托盘有「真·未识别」review 项 → 跳过，保持 review
6. 整盘混放（可乐 + 两袋乐事）+ 总重 475g → 可乐不变、乐事 70+40
7. 单袋 + 残差 60g（介于 45/75 且都超容差）→ 无法唯一确定，保持 review

运行：D:/envs/supermarketenv/python.exe tests/test_weight_resolve.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.models.schemas import Candidate, Detection, Product
from app.repositories import product_repo
from app.services.vision import (
    _extract_grams,
    _pick_sku_in_group,
    _resolve_by_weight,
)

PASSED = 0
FAILED = 0


def ok(label: str):
    global PASSED
    PASSED += 1
    print(f"  [PASS] {label}")


def fail(label: str, detail: str):
    global FAILED
    FAILED += 1
    print(f"  [FAIL] {label} -- {detail}")


def _make_det(box_id, x, y, w, h, sku_id, name, score=0.8, state="review",
              evidence="", meas=None) -> Detection:
    det = Detection(
        box_id=box_id, x=x, y=y, w=w, h=h,
        candidates=[Candidate(sku_id=sku_id, name=name, score=score)],
        quantity=1, state=state, message="test", evidence=evidence,
    )
    if meas is not None:
        det._meas = meas
    return det


products = product_repo.list_products()
products_by_sku = {p.sku_id: p for p in products}
settings = Settings()

# 乐事相似组（从 SIMILAR_GROUPS 提取）
lay_group = None
for g in __import__("app.data.products", fromlist=["SIMILAR_GROUPS"]).SIMILAR_GROUPS:
    if "SKU009" in g or "SKU010" in g or "SKU027" in g or "SKU029" in g:
        lay_group = tuple(g)
        break
assert lay_group is not None, "乐事相似组未找到"

PENDING_EVIDENCE = "疑似乐事 原味薯片（规格需人工确认，OCR未读到净含量）"


# ════════════════════════════════════════════════════════════════
# 1. 两袋乐事 + 总重 120g → 一 70g 一 40g
# ════════════════════════════════════════════════════════════════

def test_two_bags_total_120():
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    v2 = _make_det("V2", 17, 57, 24, 29, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    dets = [v1, v2]
    _resolve_by_weight(dets, 120.0, products_by_sku, settings)

    assert v1.state == "auto", f"V1 应为 auto，实际 {v1.state}"
    assert v2.state == "auto", f"V2 应为 auto，实际 {v2.state}"
    grams = []
    for d in dets:
        sku = d.candidates[0].sku_id
        g = _extract_grams(products_by_sku[sku].spec)
        grams.append(g)
    assert sorted(grams) == [40, 70], f"期望 [40,70]，实际 {sorted(grams)}"
    ok("weight: 两袋乐事+120g → 70g + 40g 组合唯一确定")


# ════════════════════════════════════════════════════════════════
# 2. 单袋歧义 + 残差 45g → 40g；残差 75g → 70g
# ════════════════════════════════════════════════════════════════

def test_single_45_resolves_40():
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    _resolve_by_weight([v1], 45.0, products_by_sku, settings)
    sku = v1.candidates[0].sku_id
    g = _extract_grams(products_by_sku[sku].spec)
    assert v1.state == "auto" and g == 40, f"期望 40g auto，实际 {g}/{v1.state}"
    ok("weight: 单袋残差45g → 40g")


def test_single_75_resolves_70():
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    _resolve_by_weight([v1], 75.0, products_by_sku, settings)
    sku = v1.candidates[0].sku_id
    g = _extract_grams(products_by_sku[sku].spec)
    assert v1.state == "auto" and g == 70, f"期望 70g auto，实际 {g}/{v1.state}"
    ok("weight: 单袋残差75g → 70g")


# ════════════════════════════════════════════════════════════════
# 3. OCR 已确认 70g 不被重量覆盖（重量仅作最后终裁）
# ════════════════════════════════════════════════════════════════

def test_ocr_confirmed_not_overridden():
    # V1 已 OCR 确认 70g（auto + evidence 含净含量）
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.85, state="auto", evidence="净含量:70克")
    # V2 仍待确认
    v2 = _make_det("V2", 17, 57, 24, 29, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    dets = [v1, v2]
    # 总重 = 70g袋(75) + 40g袋(45) = 120
    _resolve_by_weight(dets, 120.0, products_by_sku, settings)

    v1_sku = v1.candidates[0].sku_id
    v1_g = _extract_grams(products_by_sku[v1_sku].spec)
    assert v1.state == "auto" and v1_g == 70, "OCR 已确认的 70g 必须保持不动"
    assert v2.state == "auto", "V2 应被重量终裁为 auto"
    v2_g = _extract_grams(products_by_sku[v2.candidates[0].sku_id].spec)
    assert v2_g == 40, f"V2 应推断为 40g，实际 {v2_g}"
    ok("weight: OCR已确认70g不被覆盖，V2重量终裁为40g")


# ════════════════════════════════════════════════════════════════
# 4. tray_weight_g=None → 跳过
# ════════════════════════════════════════════════════════════════

def test_none_weight_skips():
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    _resolve_by_weight([v1], None, products_by_sku, settings)
    assert v1.state == "review", "无重量读数时不应改变状态"
    ok("weight: tray_weight_g=None → 跳过")


# ════════════════════════════════════════════════════════════════
# 5. 托盘有「真·未识别」review 项 → 跳过
# ════════════════════════════════════════════════════════════════

def test_unknown_review_blocks():
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    # 真·未识别：无候选、review
    unknown = _make_det("VX", 5, 5, 10, 10, "SKU010", "乐事 原味薯片",
                        score=0.0, state="review", evidence="识别到商品但无法确定品类")
    unknown.candidates = []  # 清空候选模拟未识别
    _resolve_by_weight([v1, unknown], 120.0, products_by_sku, settings)
    assert v1.state == "review", "托盘有无法归类项时应放弃重量推断"
    ok("weight: 存在未识别项 → 跳过重量终裁")


# ════════════════════════════════════════════════════════════════
# 6. 整盘混放（可乐 + 两袋乐事）+ 总重 475g → 可乐不变、乐事 70+40
# ════════════════════════════════════════════════════════════════

def test_mixed_tray_with_cola():
    cola = _make_det("C1", 60, 70, 12, 30, "SKU001", "可口可乐 汽水",
                     score=0.9, state="auto", evidence="可口可乐 330ml")
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    v2 = _make_det("V2", 17, 57, 24, 29, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    dets = [cola, v1, v2]
    # 可乐 355 + 乐事 75 + 45 = 475
    _resolve_by_weight(dets, 475.0, products_by_sku, settings)

    cola_sku = cola.candidates[0].sku_id
    assert cola_sku == "SKU001" and cola.state == "auto", "可乐必须保持不变"
    grams = []
    for d in (v1, v2):
        assert d.state == "auto"
        grams.append(_extract_grams(products_by_sku[d.candidates[0].sku_id].spec))
    assert sorted(grams) == [40, 70], f"乐事应拆成 40+70，实际 {sorted(grams)}"
    ok("weight: 整盘混放(可乐+两乐事)475g → 可乐不变 + 乐事70+40")


# ════════════════════════════════════════════════════════════════
# 7. 单袋 + 残差 60g（介于 45/75，都超容差）→ 无法唯一确定，保持 review
# ════════════════════════════════════════════════════════════════

def test_indecisive_stays_review():
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                   score=0.8, state="review", evidence=PENDING_EVIDENCE)
    _resolve_by_weight([v1], 60.0, products_by_sku, settings)
    assert v1.state == "review", "残差落在中间、无唯一组合时应保持复核"
    ok("weight: 残差60g 无法唯一确定 → 保持 review")


if __name__ == "__main__":
    print("=" * 60)
    print("  相似组重量终裁（模拟称重传感器）回归测试")
    print("=" * 60)
    test_two_bags_total_120()
    test_single_45_resolves_40()
    test_single_75_resolves_70()
    test_ocr_confirmed_not_overridden()
    test_none_weight_skips()
    test_unknown_review_blocks()
    test_mixed_tray_with_cola()
    test_indecisive_stays_review()

    print("\n" + "-" * 60)
    print(f"  结果: {PASSED} PASS / {FAILED} FAIL")
    if FAILED:
        print("  ⚠️ 有失败项！")
        sys.exit(1)
    else:
        print("  ✅ 全部通过")
        sys.exit(0)
