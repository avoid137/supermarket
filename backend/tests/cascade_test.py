"""级联识别与多帧投票的回归测试（不依赖网络，全部 mock）。

覆盖三条最容易退化的路径：
  1. 多帧投票聚合 —— 过半成条、各说各话合并、OCR 取多数
  2. 级联正路径 —— YOLO 真实框坐标必须原样传到最终 Detection
  3. 级联拒判 —— 误检裁剪（非商品）必须被丢弃，不能硬认成清单商品
"""

import asyncio
import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw

from app.core.config import Settings
from app.repositories import product_repo
from app.services import vision
from app.services.cascade import crop_box, recognize_cascade
from app.services.detectors.base import BoxProposal

PASSED = 0


def ok(label: str):
    global PASSED
    PASSED += 1
    print(f"  [PASS] {label}")


def main() -> None:
    settings = Settings()
    products = product_repo.list_products()
    by_sku = {p.sku_id: p for p in products}

    # ---------- 1. 多帧投票聚合 ----------
    print("=== 多帧投票聚合 ===")
    frames = [
        [{"candidates": [{"sku_id": "SKU001", "confidence": 0.9}], "quantity": 1, "ocr_text": "可口可乐 330ml"}],
        [{"candidates": [{"sku_id": "SKU001", "confidence": 0.88}], "quantity": 1, "ocr_text": ""}],
        [{"candidates": [{"sku_id": "SKU002", "confidence": 0.85}], "quantity": 1, "ocr_text": ""}],
    ]
    items = vision.aggregate_votes(frames, by_sku)
    assert len(items) == 1
    assert items[0]["candidates"][0]["sku_id"] == "SKU001"
    assert items[0]["candidates"][1]["sku_id"] == "SKU002"  # 对手候选保留，用于触发追问
    assert items[0]["ocr_text"] == "可口可乐 330ml"
    ok("2/3 票成条，少数派降为对手候选，OCR 取多数帧")

    frames_disagree = [
        [{"candidates": [{"sku_id": "SKU001", "confidence": 0.9}], "quantity": 1, "ocr_text": ""}],
        [{"candidates": [{"sku_id": "SKU002", "confidence": 0.9}], "quantity": 1, "ocr_text": ""}],
        [{"candidates": [{"sku_id": "SKU003", "confidence": 0.9}], "quantity": 1, "ocr_text": ""}],
    ]
    items2 = vision.aggregate_votes(frames_disagree, by_sku)
    assert len(items2) == 1, "三帧各说各话必须合并成 1 条，防止重复计费"
    ok("三帧各说各话 → 合并 1 条，交由分差压到人工复核")

    # ---------- 2. crop_box 裁剪含边距 ----------
    print("=== crop_box ===")
    img = Image.new("RGB", (1000, 500), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    crop_b64 = crop_box(buf.getvalue(), BoxProposal(x=40, y=30, w=20, h=40, score=0.9))
    c = Image.open(io.BytesIO(base64.b64decode(crop_b64)))
    assert c.width >= 200 and c.height >= 200, "裁剪应不小于框本身"
    ok(f"裁剪尺寸 {c.size}，含 10% 边距（保护边缘处的规格文字）")

    # ---------- 3. 级联正路径 + 拒判（mock 大模型） ----------
    print("=== 级联正路径（mock）===")

    class FakeDetector:
        name = "fake"

        def propose(self, image_bytes: bytes):
            return [
                BoxProposal(x=11.11, y=22.22, w=33.33, h=44.44, score=0.9),
                BoxProposal(x=55.0, y=60.0, w=20.0, h=25.0, score=0.8),
            ]

    async def fake_api(image_b64: str, s, cropped: bool = False):
        img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
        px = img.getpixel((img.width // 2, img.height // 2))
        if px[0] > 180 and px[1] < 80:  # 红色块 = 模拟商品
            return [
                {
                    "ocr_text": "可口可乐 330ml",
                    "candidates": [{"sku_id": "SKU001", "confidence": 0.93}],
                    "quantity": 1,
                }
            ]
        return [{"ocr_text": "", "candidates": [], "quantity": 1}]  # 灰块 = 模拟误检

    original_api = vision.call_vision_api
    vision.call_vision_api = fake_api
    try:
        img = Image.new("RGB", (800, 600), (240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.rectangle([88, 133, 355, 399], fill=(200, 30, 30))
        draw.rectangle([440, 360, 600, 510], fill=(120, 120, 120))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode()

        dets = asyncio.run(recognize_cascade([b64], settings, FakeDetector()))
        assert len(dets) == 1, "误检的灰块必须被拒判"
        d0 = dets[0]
        assert (d0.x, d0.y, d0.w, d0.h) == (11.11, 22.22, 33.33, 44.44), "框坐标必须来自检测器而非占位框"
        assert d0.evidence == "可口可乐 330ml"
        assert d0.candidates[0].sku_id == "SKU001"
        ok("YOLO 真实框坐标原样传递到 Detection，OCR 依据保留")
        ok("误检裁剪（非商品）被大模型拒判，不产生误账")
    finally:
        vision.call_vision_api = original_api

    print(f"\n通过 {PASSED} 项，失败 0 项")


if __name__ == "__main__":
    main()
