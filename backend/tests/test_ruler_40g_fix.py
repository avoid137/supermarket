"""40g/70g 乐事薯片「尺子/OCR 纠偏」回归测试（不依赖网络，使用真实商品数据）。

修复背景
--------
视觉模型面对外观一致的乐事 70g / 40g 薯片，常常只输出 70g 一个候选。
旧的「候选内重排（需 >=2 候选）」和「级联合并（需 40g 与 70g 同时出现）」都救不回来，
导致 40g 稳定被识别成 70g。

新方案做成「能纠正单一候选的硬纠偏」：只要 top 候选属于某个相似组，就用两类更可靠的
信号重选款——
  1) OCR 净含量：包装印的「40g/70g」印刷体数字，比看外观可靠；
  2) 尺子实测长度：YOLO 框像素 / px_per_cm 得到商品外尺寸长边，与组内各款 pkg_length_cm 比对。
两个信号都要求「切到组内另一款」才生效，否则信任视觉，避免噪声误翻转。

本测试覆盖最关键的 5 个场景，确保该修复不退化为「永远偏 70g」或「误翻 70g」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.repositories import product_repo
from app.services.vision import _resolve_similar_group, build_detections

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


def main() -> None:
    settings = Settings()
    by_sku = {p.sku_id: p for p in product_repo.list_products()}

    # 数据自检：SKU009=70g, SKU029=40g，且都有外尺寸标定
    assert "70" in (by_sku["SKU009"].spec or ""), "SKU009 应为 70g"
    assert "40" in (by_sku["SKU029"].spec or ""), "SKU029 应为 40g"
    assert (by_sku["SKU009"].visual.pkg_length_cm or 0) > 0
    assert (by_sku["SKU029"].visual.pkg_length_cm or 0) > 0

    # 1) 单一 70g 候选 + 尺子实测 20cm -> 应翻转为 40g(SKU029)
    sku, reason = _resolve_similar_group("SKU009", 20.0, "", by_sku)
    if sku == "SKU029" and reason and "尺子" in reason:
        ok(f"单一70g候选+尺子20cm -> {sku} ({reason})")
    else:
        fail("单一70g候选+尺子20cm", f"got {sku} {reason}")

    # 2) 单一 70g 候选 + OCR '40g' -> SKU029（OCR 净含量优先）
    sku, reason = _resolve_similar_group("SKU009", None, "乐事 薯片 40g", by_sku)
    if sku == "SKU029" and reason and "OCR" in reason:
        ok(f"单一70g候选+OCR'40g' -> {sku} ({reason})")
    else:
        fail("单一70g候选+OCR40g", f"got {sku} {reason}")

    # 3) 单一 40g 候选 + 尺子 22cm -> 保持 70g(SKU009)，不误翻
    sku, reason = _resolve_similar_group("SKU029", 22.0, "", by_sku)
    if sku == "SKU009":
        ok(f"单一40g候选+尺子22cm -> 保持 {sku}（未误翻）")
    else:
        fail("单一40g候选+尺子22cm", f"got {sku} {reason}")

    # 4) 边界 21cm -> 维持 70g（差值不足阈值，信任视觉）
    sku, reason = _resolve_similar_group("SKU009", 21.0, "", by_sku)
    if sku == "SKU009":
        ok(f"尺子21cm边界 -> 维持 {sku}")
    else:
        fail("尺子21cm边界", f"got {sku} {reason}")

    # 5) build_detections 端到端：单一 SKU009 候选 + lengths=[20.0] -> 顶层 SKU029
    raw = [{"candidates": [{"sku_id": "SKU009", "confidence": 0.92}], "quantity": 1, "ocr_text": ""}]
    dets = build_detections(raw, by_sku, [(10.0, 30.0, 18.0, 20.0)], settings, lengths=[20.0])
    if dets and dets[0].candidates and dets[0].candidates[0].sku_id == "SKU029":
        ok(
            f"build_detections 端到端: 顶层 {dets[0].candidates[0].sku_id}, "
            f"state={dets[0].state}, evidence={dets[0].evidence!r}"
        )
    else:
        got = [(d.candidates[0].sku_id if d.candidates else None) for d in dets]
        fail("build_detections 端到端", f"got {got}")

    print(f"\n结果: {PASSED} 通过, {FAILED} 失败")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
