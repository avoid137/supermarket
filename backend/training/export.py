#!/usr/bin/env python3
"""把训练好的 best.pt 导出为 ONNX，供后端 detectors/yolo.py 加载推理。

用法（在 backend/ 目录下）：
    python training/export.py --weights training/runs/detect/weights/best.pt
    python training/export.py --weights training/runs/classify/weights/best.pt --task classify
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 YOLO 为 ONNX")
    parser.add_argument("--weights", required=True, help="best.pt 路径")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--opset", type=int, default=12, help="onnxruntime 兼容性，默认 12 较稳")
    args = parser.parse_args()

    weights = Path(args.weights)
    if not weights.exists():
        raise SystemExit(f"权重不存在: {weights}")

    try:
        from ultralytics import YOLO
    except ImportError:
        raise SystemExit("未安装 ultralytics，请先 pip install ultralytics")

    model = YOLO(str(weights))
    exported = model.export(format="onnx", imgsz=args.imgsz, opset=args.opset, dynamic=True)
    print(f"\n导出成功: {exported}")


if __name__ == "__main__":
    main()
