#!/usr/bin/env python3
"""把常见标注格式转换成 YOLO 检测用的 txt 格式。

支持两种标注工具的产物，按文件扩展名自动识别：
  - labelImg / 自研工具的 Pascal VOC XML（.xml）
  - labelme 的 JSON（.json）

输出 YOLO 格式：每张图一个同名 .txt，每行 ``class_id cx cy w h``（归一化到 0~1）。
class 名必须是 sku_id（如 SKU001），脚本用 class_map.json 里的映射转成 class_id。

用法（在 backend/ 目录下）：
    python training/scripts/convert_annotations.py --input training/datasets/raw/SKU001
    python training/scripts/convert_annotations.py --input training/datasets/raw   # 递归扫描全部 SKU
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
TRAINING_DIR = Path(__file__).resolve().parents[1]
CLASS_MAP_PATH = TRAINING_DIR / "class_map.json"


def load_sku_to_class() -> dict[str, int]:
    if not CLASS_MAP_PATH.exists():
        raise SystemExit(f"找不到 {CLASS_MAP_PATH}，请先运行 scripts/make_catalog.py")
    data = json.loads(CLASS_MAP_PATH.read_text(encoding="utf-8"))
    return data["sku_to_class"]


def voc_to_yolo(xml_path: Path, sku_to_class: dict[str, int]) -> str | None:
    """解析 VOC XML，返回 YOLO txt 内容；无有效框时返回 None。"""
    root = ET.parse(xml_path).getroot()
    size = root.find("size")
    if size is None:
        return None
    width = float(size.findtext("width"))
    height = float(size.findtext("height"))
    if width <= 0 or height <= 0:
        return None

    lines: list[str] = []
    for obj in root.findall("object"):
        name = (obj.findtext("name") or "").strip()
        if name not in sku_to_class:
            print(f"  [跳过] {xml_path.name}: 标注名 '{name}' 不在 class_map，忽略该框")
            continue
        bndbox = obj.find("bndbox")
        if bndbox is None:
            continue
        xmin = float(bndbox.findtext("xmin"))
        ymin = float(bndbox.findtext("ymin"))
        xmax = float(bndbox.findtext("xmax"))
        ymax = float(bndbox.findtext("ymax"))
        cx = (xmin + xmax) / 2 / width
        cy = (ymin + ymax) / 2 / height
        w = (xmax - xmin) / width
        h = (ymax - ymin) / height
        lines.append(f"{sku_to_class[name]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return "\n".join(lines) if lines else None


def labelme_to_yolo(json_path: Path, sku_to_class: dict[str, int]) -> str | None:
    """解析 labelme JSON，返回 YOLO txt 内容；无有效框时返回 None。"""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    width = float(data.get("imageWidth") or 0)
    height = float(data.get("imageHeight") or 0)
    if width <= 0 or height <= 0:
        return None

    lines: list[str] = []
    for shape in data.get("shapes", []):
        label = (shape.get("label") or "").strip()
        if label not in sku_to_class:
            print(f"  [跳过] {json_path.name}: 标注名 '{label}' 不在 class_map，忽略该框")
            continue
        points = shape.get("points") or []
        if len(points) < 2:
            continue
        (x1, y1), (x2, y2) = points[0], points[1]
        xmin, xmax = sorted((x1, x2))
        ymin, ymax = sorted((y1, y2))
        cx = (xmin + xmax) / 2 / width
        cy = (ymin + ymax) / 2 / height
        w = (xmax - xmin) / width
        h = (ymax - ymin) / height
        lines.append(f"{sku_to_class[label]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return "\n".join(lines) if lines else None


def convert_dir(target: Path, sku_to_class: dict[str, int]) -> tuple[int, int]:
    """递归扫描目录，转换所有 .xml / .json，返回 (转换数, 跳过数)。"""
    converted = skipped = 0
    for ann in sorted(target.rglob("*")):
        if ann.suffix.lower() == ".xml":
            content = voc_to_yolo(ann, sku_to_class)
        elif ann.suffix.lower() == ".json":
            content = labelme_to_yolo(ann, sku_to_class)
        else:
            continue

        txt_path = ann.with_suffix(".txt")
        if content is None:
            skipped += 1
            continue
        txt_path.write_text(content, encoding="utf-8")
        converted += 1
    return converted, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="标注格式转 YOLO txt")
    parser.add_argument("--input", required=True, help="标注文件或目录（.xml / .json，递归扫描）")
    args = parser.parse_args()

    sku_to_class = load_sku_to_class()
    target = Path(args.input).resolve()
    if not target.exists():
        raise SystemExit(f"路径不存在: {target}")

    if target.is_file():
        converted, skipped = convert_dir(target, sku_to_class)
    else:
        converted, skipped = convert_dir(target, sku_to_class)

    print(f"完成：转换 {converted} 个标注文件，跳过 {skipped} 个（无有效框）")


if __name__ == "__main__":
    main()
