"""级联「按框实例」聚合回归测试（mock 检测器 + mock 视觉模型，不依赖真实模型/权重）。

覆盖修复后的核心行为：
  1. 单帧两包相同薯片（2 个不重叠框） -> 2 条独立 Detection（不再被同 SKU 合并吞掉一包）
  2. 单帧两包不同规格（70g + 40g，2 个框） -> 2 条（不再被相似组合并强行合一条）
  3. 连拍多帧同一商品（每帧 1 个同位置框） -> 聚类成 1 条（不重复计数）
"""
import asyncio
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw

from app.core.config import Settings
from app.services.detectors.base import BoxProposal
from app.services import cascade, vision

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


def _red_block() -> str:
    img = Image.new("RGB", (800, 600), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    draw.rectangle([88, 133, 355, 399], fill=(200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _resp(sku: str) -> list:
    return [{"ocr_text": "", "candidates": [{"sku_id": sku, "confidence": 0.9}], "quantity": 1}]


def _make_api(sequence: list):
    seq = list(sequence)

    async def _api(image_b64, s, cropped=False):
        return seq.pop(0)

    return _api


async def _ruler(*a, **k):
    return None


def main() -> None:
    settings = Settings()
    # 关掉整帧补读，避免它按调用顺序消费 side_effect，干扰「按框」用例的断言
    settings.CASCADE_FULLFRAME_FALLBACK = False

    def make_detector(boxes):
        class D:
            name = "fake"

            def propose(self, image_bytes):
                return list(boxes)

        return D()

    two_boxes = [
        BoxProposal(x=10, y=30, w=20, h=20),
        BoxProposal(x=70, y=30, w=20, h=20),
    ]
    one_box = [BoxProposal(x=10, y=30, w=20, h=20)]

    # ---------- 1. 单帧两包同款 -> 2 条 ----------
    print("=== 单帧两包同款 -> 2 条 ===")
    with patch("app.services.vision.call_vision_api", new=_make_api([_resp("SKU009"), _resp("SKU009")])), \
         patch("app.services.vision.probe_ruler_scale", new=_ruler):
        dets = asyncio.run(cascade.recognize_cascade([_red_block()], settings, make_detector(two_boxes)))
    if dets and len(dets) == 2 and all(d.candidates[0].sku_id == "SKU009" for d in dets):
        ok(f"两包同款 -> {len(dets)} 条（均为 SKU009，未被合并吞件）")
    else:
        fail("两包同款", f"got {[(d.candidates[0].sku_id if d.candidates else None) for d in (dets or [])]}")

    # ---------- 2. 单帧两包不同规格 -> 2 条 ----------
    print("=== 单帧两包不同规格(70g+40g) -> 2 条 ===")
    with patch("app.services.vision.call_vision_api", new=_make_api([_resp("SKU009"), _resp("SKU029")])), \
         patch("app.services.vision.probe_ruler_scale", new=_ruler):
        dets = asyncio.run(cascade.recognize_cascade([_red_block()], settings, make_detector(two_boxes)))
    if dets and len(dets) == 2:
        skus = {d.candidates[0].sku_id for d in dets}
        if skus == {"SKU009", "SKU029"}:
            ok(f"70g+40g -> 2 条，分别为 {skus}（未被相似组合并吞件）")
        else:
            fail("70g+40g", f"skus={skus}")
    else:
        fail("70g+40g", f"len={len(dets or [])}")

    # ---------- 3. 连拍多帧同商品 -> 聚类成 1 条 ----------
    print("=== 连拍多帧同商品 -> 聚类成 1 条 ===")
    with patch("app.services.vision.call_vision_api", new=_make_api([_resp("SKU009"), _resp("SKU009")])), \
         patch("app.services.vision.probe_ruler_scale", new=_ruler):
        dets = asyncio.run(
            cascade.recognize_cascade([_red_block(), _red_block()], settings, make_detector(one_box))
        )
    if dets and len(dets) == 1 and dets[0].candidates[0].sku_id == "SKU009":
        ok("连拍 2 帧同位置同商品 -> 1 条（未重复计数）")
    else:
        fail("连拍多帧", f"got {len(dets or [])} 条")

    print(f"\n结果: {PASSED} 通过, {FAILED} 失败")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
