#!/usr/bin/env python3
"""YOLO 训练入口：封装 ultralytics，检测 / 分类两条路线。

检测（解决「占位框 → 真实 bbox」）：
    python training/train.py --task detect --model yolov8n.pt --epochs 100

分类（先用最小成本验证「乐事 40g vs 70g 能不能分开」）：
    python training/train.py --task classify --model yolov8n-cls.pt --epochs 50

依赖：pip install ultralytics（会带 torch）。训练前先跑 split_dataset.py 准备好数据。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TRAINING_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TRAINING_DIR.parent  # backend/
CLASS_MAP_PATH = TRAINING_DIR / "class_map.json"
DETECT_DIR = TRAINING_DIR / "datasets" / "detect"
CLASSIFY_DIR = TRAINING_DIR / "datasets" / "classify"
DEFAULT_DETECT_WTS = BACKEND_DIR / "models" / "yolov8n.pt"


def load_class_map() -> dict:
    if not CLASS_MAP_PATH.exists():
        raise SystemExit(f"找不到 {CLASS_MAP_PATH}，请先运行 scripts/make_catalog.py")
    return json.loads(CLASS_MAP_PATH.read_text(encoding="utf-8"))


def build_detect_data(cm: dict) -> dict:
    """构造检测任务的 data 字典（绝对路径 + sku_id 命名），不依赖 data.yaml 的路径解析。"""
    return {
        "path": DETECT_DIR.as_posix(),  # 用正斜杠，避免 Windows 反斜杠在路径拼接时出问题
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {c["class_id"]: c["sku_id"] for c in cm["classes"]},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="训练 YOLO 模型")
    parser.add_argument("--task", choices=["detect", "classify"], default="detect")
    parser.add_argument("--model", default=None, help="预训练权重，留空按任务自动选 yolov8n / yolov8n-cls")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--freeze", type=int, default=0, help="冻结前 N 层骨干先训头，微调时建议 10")
    parser.add_argument("--device", default="", help="0 / cpu，留空自动")
    parser.add_argument("--project", default=str(TRAINING_DIR / "runs"))
    args = parser.parse_args()

    cm = load_class_map()
    if args.model:
        model_path = args.model
    elif args.task == "detect":
        model_path = str(DEFAULT_DETECT_WTS)  # 默认指向 backend/models/yolov8n.pt（已下载）
    else:
        model_path = "yolov8n-cls.pt"  # 分类路线需先下载该权重（见 README）

    try:
        from ultralytics import YOLO
    except ImportError:
        raise SystemExit(
            "未安装 ultralytics。请先执行：\n"
            "  D:/envs/supermarketenv/python.exe -m pip install ultralytics"
        )

    device = args.device or None
    model = YOLO(model_path)

    if args.task == "detect":
        data = build_detect_data(cm)
        print(f"检测训练：model={model_path}, 类别={cm['num_classes']}, epochs={args.epochs}")
        model.train(
            data=data, epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
            freeze=args.freeze, device=device, project=args.project, name="detect",
        )
    else:
        print(f"分类训练：model={model_path}, 类别={cm['num_classes']}, epochs={args.epochs}")
        model.train(
            data=str(CLASSIFY_DIR), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
            freeze=args.freeze, device=device, project=args.project, name="classify",
        )

    print("\n训练完成。最佳权重在 runs/<task>/weights/best.pt")
    print("下一步：python training/export.py 导出 ONNX")


if __name__ == "__main__":
    main()
