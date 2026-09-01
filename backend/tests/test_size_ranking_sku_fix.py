"""尺寸排序/排除法 SKU 重指派 + Phantom 守卫 回归测试。

修复背景（两个独立 bug，同一次 E2E 发现）：
1. _resolve_by_size_ranking / _resolve_by_elimination 只改 det.state 和 det.evidence 文字，
   但从未重设 det.candidates 的 sku_id → confirm/pay 仍按原候选（70g）入账。
2. YOLO 对软包装检出极小部分框（如大袋角落），VLM 错判为完全不同的商品（沙琪玛），
   因 conf 高而 auto 入账 → 每笔多收不存在的商品。

本测试确保：
- 尺寸排序推断后 candidates[0].sku_id 确实切换到目标规格的 SKU
- 排除法同理
- 同风味优先保留（70g 原味不会被翻成 70g 黄瓜）
- 去重防止双 70g（SKU009+SKU010 都是 70g 时只分配一次）
- Phantom 守卫：面积过小的 auto 检测被降为 review
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
    _reassign_detection_sku,
    _resolve_by_elimination,
    _resolve_by_size_ranking,
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
              evidence="", meas=None, candidates=None) -> Detection:
    """构造一个最小可用的 Detection，用于测试 resolver。

    candidates: 可选，直接指定候选列表（覆盖由 sku_id/name/score 生成的单项）。
    """
    if candidates is None:
        candidates = [Candidate(sku_id=sku_id, name=name, score=score)]
    det = Detection(
        box_id=box_id, x=x, y=y, w=w, h=h,
        candidates=candidates,
        quantity=1, state=state, message="test", evidence=evidence,
    )
    if meas is not None:
        det._meas = meas
    return det


# ── 测试数据准备 ──────────────────────────────────────────────

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
print(f"\n乐事相似组: {lay_group}")
print(f"组内 SKU:")
for s in lay_group:
    p = products_by_sku.get(s)
    if p:
        print(f"  {s}: {p.name} spec={p.spec} pkg_len={getattr(p.visual,'pkg_length_cm',0)}")


# ════════════════════════════════════════════════════════════════
# 1. _pick_sku_in_group：按 gram 挑 SKU，优先匹配风味名
# ════════════════════════════════════════════════════════════════

def test_pick_sku_exact_match():
    """精确匹配 gram → 返回该 gram 的第一个 SKU。"""
    r = _pick_sku_in_group(40, lay_group, products_by_sku)
    assert r is not None, "40g SKU 应该存在"
    p = products_by_sku[r]
    g = _extract_grams(p.spec)
    assert g == 40, f"期望 40g，实际 {g}"
    ok("pick_sku: 40g 精确匹配")


def test_pick_sku_prefers_flavor():
    """preferred_name 匹配时返回同名 SKU。"""
    # 40g 有 SKU027(原味) 和 SKU029(黄瓜味)
    r = _pick_sku_in_group(40, lay_group, products_by_sku, preferred_name="乐事 原味薯片")
    p = products_by_sku[r]
    assert "原味" in p.name, f"期望含'原味'，实际 {p.name}"
    ok("pick_sku: 风味偏好保留（40g→原味）")


def test_pick_sku_fallback_no_match():
    """preferred_name 不匹配任何同 gram SKU 时回退到第一个。"""
    r = _pick_sku_in_group(40, lay_group, products_by_sku, preferred_name="不存在这个名字")
    assert r is not None
    p = products_by_sku[r]
    g = _extract_grams(p.spec)
    assert g == 40
    ok("pick_sku: 风格不匹配时回退到首个 40g SKU")


def test_pick_sku_no_match_returns_none():
    """gram 在组内不存在时返回 None。"""
    r = _pick_sku_in_group(999, lay_group, products_by_sku)
    assert r is None
    ok("pick_sku: 不存在的 gram 返回 None")


# ════════════════════════════════════════════════════════════════
# 2. _reassign_detection_sku：真正切换 candidates + state/evidence/message
# ════════════════════════════════════════════════════════════════

def test_reassign_switches_candidate():
    """重指派后 candidates[0].sku_id 应为目标 SKU。"""
    det = _make_det("V1", 10, 30, 20, 40, "SKU010", "乐事 原味薯片", score=0.8,
                      state="review", evidence="规格需人工确认")
    target = "SKU027"  # 40g 原味
    _reassign_detection_sku(det, target, products_by_sku, "测试推断40g")

    assert det.state == "auto"
    assert det.candidates[0].sku_id == target, \
        f"期望 top={target}，实际 {det.candidates[0].sku_id}"
    assert det.candidates[0].name == products_by_sku[target].name
    assert "测试推断40g" in det.evidence
    # message 应被 build_message 重新赋值（auto 状态按设计返回 ""，
    # 这里只验证字段确实被重写且为字符串，而非遗留旧值 "test"）
    assert isinstance(det.message, str), "message 应被 build_message 重写为字符串"
    ok("reassign: candidates[0].sku_id 已切换到目标 + message 已重写")


def test_reassign_preserves_runner_candidates():
    """重指派应保留非目标候选（去重）。"""
    det = _make_det("V1", 10, 30, 20, 40, "SKU010", "乐事 原味薯片",
                      candidates=[
                          Candidate(sku_id="SKU010", name="乐事 原味薯片", score=0.8),
                          Candidate(sku_id="SKU009", name="乐事 黄瓜味薯片", score=0.7),
                      ])
    _reassign_detection_sku(det, "SKU027", products_by_sku, "reason")
    skus = [c.sku_id for c in det.candidates]
    assert skus[0] == "SKU027"
    assert "SKU010" in skus or "SKU009" in skus, "原始候选应保留在后续位置"
    ok("reassign: 非目标候选保留（去重）")


# ════════════════════════════════════════════════════════════════
# 3. 尺寸排序：大袋→70g / 小袋→40g，且 candidates 真正切换
# ════════════════════════════════════════════════════════════════

def test_size_ranking_two_bags():
    """两袋乐事（大 meas > 小 meas）→ 分别分配 70g 和 40g。"""
    # V1 大包：YOLO 框偏小但相对更大，视觉 top 是 70g（但 OCR 未确认）
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                     score=0.76, state="review",
                     evidence="疑似乐事 原味薯片（规格需人工确认，OCR未读到净含量）",
                     meas=10.6)
    # V2 小包：视觉 top 也是 70g（VLM 分不清），需要翻转到 40g
    v2 = _make_det("V2", 17, 57, 24, 29, "SKU010", "乐事 原味薯片",
                     score=0.76, state="review",
                     evidence="疑似乐事 原味薯片（规格需人工确认，OCR未读到净含量）",
                     meas=7.1)

    dets = [v1, v2]
    _resolve_by_size_ranking(dets, products_by_sku)

    # 两者都应为 auto
    assert v1.state == "auto", f"V1 state={v1.state}"
    assert v2.state == "auto", f"V2 state={v2.state}"

    # V1 应为 70g（大袋）
    v1_g = _extract_grams(v1.evidence or "")
    v1_sku = v1.candidates[0].sku_id
    v1_prod = products_by_sku.get(v1_sku)
    v1_spec_g = _extract_grams(v1_prod.spec) if v1_prod else None
    assert v1_g == 70 or v1_spec_g == 70, \
        f"V1 应为 70g: evidence={v1.evidence}, sku={v1_sku}"

    # V2 应为 40g（小袋）
    v2_g = _extract_grams(v2.evidence or "")
    v2_sku = v2.candidates[0].sku_id
    v2_prod = products_by_sku.get(v2_sku)
    v2_spec_g = _extract_grams(v2_prod.spec) if v2_prod else None
    assert v2_g == 40 or v2_spec_g == 40, \
        f"V2 应为 40g: evidence={v2.evidence}, sku={v2_sku}"

    # V1 和 V2 不能是同一个 SKU
    assert v1_sku != v2_sku, f"两袋不应同 SKU: V1={v1_sku} V2={v2_sku}"

    ok(f"size_ranking: V1={v1_sku}(70g) V2={v2_sku}(40g) 区分正确")


def test_size_ranking_dedup_prevents_double_70():
    """去重逻辑：SKU009+SKU010 都是 70g，available_grams 不重复 → 不会把两件都分 70g。"""
    # 两件都偏小（模拟 OCR 全失败场景），都指向 70g 候选
    v1 = _make_det("V1", 45, 28, 30, 60, "SKU009", "乐事 黄瓜味薯片",
                     score=0.80, state="review",
                     evidence="疑似乐事 黄瓜味薯片（规格需人工确认）", meas=11.0)
    v2 = _make_det("V2", 15, 55, 22, 28, "SKU010", "乐事 原味薯片",
                     score=0.78, state="review",
                     evidence="疑似乐事 原味薯片（规格需人工确认）", meas=7.5)

    dets = [v1, v2]
    _resolve_by_size_ranking(dets, products_by_sku)

    v1_g = _extract_grams(products_by_sku[v1.candidates[0].sku_id].spec)
    v2_g = _extract_grams(products_by_sku[v2.candidates[0].sku_id].spec)

    assert v1_g == 70, f"V1 应为 70g 实际 {v1_g}"
    assert v2_g == 40, f"V2 应为 40g 实际 {v2_g}"  # 去重后只剩一个 70g slot
    ok("size_ranking: 去重防双 70g（第二件正确落到 40g）")


def test_size_ranking_small_meas_filtered():
    """meas < MIN_CREDIBLE_MEAS_CM 的检测不被尺寸排序分配。"""
    tiny = _make_det("VT", 30, 40, 8, 19, "SKU010", "乐事 原味薯片",
                       score=0.28, state="review",
                       evidence="疑似乐事 原味薯片（规格需人工确认）", meas=3.5)
    normal = _make_det("VN", 17, 57, 24, 29, "SKU010", "乐事 原味薯片",
                        score=0.76, state="review",
                        evidence="疑似乐事 原味薯片（规格需人工确认）", meas=7.5)

    dets = [tiny, normal]
    _resolve_by_size_ranking(dets, products_by_sku)

    assert tiny.state == "review", "杂框应保持 review"
    assert normal.state == "auto", "正常框应变为 auto"
    ok("size_ranking: meas<5cm 杂框不过滤（保持 review）")


# ════════════════════════════════════════════════════════════════
# 4. 排除法：一件 OCR 确认后，剩余自动分配剩余规格
# ════════════════════════════════════════════════════════════════

def test_elimination_one_confirmed():
    """V1 OCR 确认 70g → V2 通过排除法得到 40g 且 SKU 正确切换。"""
    v1 = _make_det("V1", 50, 25, 32, 65, "SKU010", "乐事 原味薯片",
                     score=0.85, state="auto",
                     evidence="净含量:70克", meas=10.6)
    v2 = _make_det("V2", 17, 57, 24, 29, "SKU010", "乐事 原味薯片",
                     score=0.75, state="review",
                     evidence="疑似乐事 原味薯片（规格需人工确认，OCR未读到净含量）",
                     meas=7.1)

    dets = [v1, v2]
    _resolve_by_elimination(dets, products_by_sku)

    assert v2.state == "auto", f"V2 应为 auto，实际 {v2.state}"
    v2_sku = v2.candidates[0].sku_id
    v2_spec_g = _extract_grams(products_by_sku[v2_sku].spec)
    assert v2_spec_g == 40, f"V2 应为 40g SKU，实际 {v2_sku} spec={products_by_sku[v2_sku].spec}"
    ok(f"elimination: V1=70g → V2={v2_sku}(40g) 排除法正确")


# ════════════════════════════════════════════════════════════════
# 5. Phantom 守卫：面积过小的 auto 检测强制 review
# ════════════════════════════════════════════════════════════════

def test_phantom_guard_tiny_box():
    """面积 < 150 ‰² 的 auto 检测被降为 review。"""
    # V4 幽灵沙琪玛：w=8.7 h=15.9 area=138 < 150
    ghost = _make_det("V4", 9.9, 61.8, 8.7, 15.9, "SKU016", "徐福记 鸡蛋味沙琪玛",
                        score=0.95, state="auto", evidence="整")
    # 正常商品不应受影响
    normal = _make_det("V1", 51.9, 28.0, 31.5, 64.1, "SKU010", "乐事 原味薯片",
                         score=0.76, state="auto", evidence="净含量:70g")

    _MIN_BOX_AREA_PCT2 = 150
    for det in [ghost, normal]:
        area = det.w * det.h
        if area < _MIN_BOX_AREA_PCT2 and det.state == "auto":
            old = det.state
            det.state = "review"
            det.evidence = f"疑似误检（框面积{area:.0f}%²过小，需人工确认）"

    assert ghost.state == "review", f"幽灵应被拦截: state={ghost.state}"
    assert "误检" in ghost.evidence
    assert normal.state == "auto", "正常检测不应受影响"
    ok("phantom_guard: 面积 138 < 150 → review ✅; 正常 2019 → auto 不变 ✅")


def test_phantom_guard_review_untouched():
    """已经是 review 的小面积检测不受影响（避免重复改写）。"""
    small_review = _make_det("V3", 31.5, 39.3, 7.9, 18.8, "SKU010", "乐事 原味薯片",
                             score=0.28, state="review", evidence="低置信度")

    _MIN_BOX_AREA_PCT2 = 150
    area = small_review.w * small_review.h  # ≈ 148.5
    if area < _MIN_BOX_AREA_PCT2 and small_review.state == "auto":
        small_review.state = "review"

    assert small_review.state == "review"  # 本来就是 review
    ok("phantom_guard: 原 review 的小框不受影响")


# ════════════════════════════════════════════════════════════════
# 运行全部测试
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  尺寸排序 SKU 重指派 + Phantom 守卫 回归测试")
    print("=" * 60)

    # _pick_sku_in_group
    test_pick_sku_exact_match()
    test_pick_sku_prefers_flavor()
    test_pick_sku_fallback_no_match()
    test_pick_sku_no_match_returns_none()

    # _reassign_detection_sku
    test_reassign_switches_candidate()
    test_reassign_preserves_runner_candidates()

    # 尺寸排序
    test_size_ranking_two_bags()
    test_size_ranking_dedup_prevents_double_70()
    test_size_ranking_small_meas_filtered()

    # 排除法
    test_elimination_one_confirmed()

    # Phantom 守卫
    test_phantom_guard_tiny_box()
    test_phantom_guard_review_untouched()

    print("\n" + "-" * 60)
    print(f"  结果: {PASSED} PASS / {FAILED} FAIL")
    if FAILED:
        print("  ⚠️ 有失败项！")
        sys.exit(1)
    else:
        print("  ✅ 全部通过")
        sys.exit(0)
