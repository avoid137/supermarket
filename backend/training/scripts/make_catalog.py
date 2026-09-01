#!/usr/bin/env python3
"""从商品主数据生成 YOLO 训练的 class 映射与数据集配置。

class 映射的**唯一事实来源**是 ``app/data/products.py``，运行本脚本即可重新生成，
避免手写两份清单随商品增减而漂移（加 SKU025~028 时就吃过这个亏）。

生成产物（均写入 training/ 目录）：
  - class_map.json   : class_id <-> sku_id 双向映射，推理接入层（detectors/yolo.py）直接用
  - data.yaml        : 检测任务配置，names 直接填 sku_id，推理时 class_id 即为 sku_id

用法：在 backend/ 目录下执行
    python training/scripts/make_catalog.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]   # backend/
TRAINING_DIR = Path(__file__).resolve().parents[1]  # backend/training/

sys.path.insert(0, str(BACKEND_DIR))

from app.data.products import PRODUCTS, SIMILAR_GROUPS  # noqa: E402


def build_class_map(sku_filter: set[str] | None = None) -> list[dict]:
    """按 sku_id 排序分配 class_id，保证稳定可复现。

    sku_filter: 传入非空集合时只保留这些 sku_id（子集训练用），
                让 class_id 从 0 连续编号，避免训出「空类」。
    """
    items = sorted(PRODUCTS, key=lambda p: p.sku_id)
    if sku_filter is not None:
        known = {p.sku_id for p in items}
        missing = sorted(sku_filter - known)
        if missing:
            print(f"[警告] 以下 sku_id 不在商品库中，已忽略: {missing}")
        items = [p for p in items if p.sku_id in sku_filter]
    return [
        {
            "class_id": i,
            "sku_id": p.sku_id,
            "name": p.name,
            "spec": p.spec,
            "category": p.category,
        }
        for i, p in enumerate(items)
    ]


def write_class_map(class_map: list[dict]) -> Path:
    """输出 class_map.json：双向映射 + 相似组 + 完整信息，供推理接入层使用。"""
    sku_to_class = {c["sku_id"]: c["class_id"] for c in class_map}
    out = {
        "num_classes": len(class_map),
        "classes": class_map,
        "sku_to_class": sku_to_class,
        "class_to_sku": {str(c["class_id"]): c["sku_id"] for c in class_map},
        "class_info": {str(c["class_id"]): c for c in class_map},
        # 相似组转成 class_id 形式，供级联仲裁层判断「是否该降级交大模型」。
        # 只保留 >=2 个成员的组（子集训练时，组里成员可能被过滤掉，只剩 0~1 个就没有歧义价值）
        "similar_groups": [
            ids
            for group in SIMILAR_GROUPS
            if len(ids := [sku_to_class[sku] for sku in group if sku in sku_to_class]) >= 2
        ],
    }
    path = TRAINING_DIR / "class_map.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_data_yaml(class_map: list[dict]) -> Path:
    """检测任务配置。names 直接填 sku_id，推理层拿到 class_id 即得 sku_id，无需查表。"""
    lines = [
        "# 由 scripts/make_catalog.py 自动生成，勿手改（改商品后重跑本脚本）",
        "# 路径相对 training/ 目录；train.py 运行时会把 path 解析成绝对路径",
        "path: datasets/detect",
        "train: images/train",
        "val: images/val",
        "test: images/test",
        "",
        "names:",
    ]
    for c in class_map:
        lines.append(f"  {c['class_id']}: {c['sku_id']}")
    lines.append("")

    path = TRAINING_DIR / "data.yaml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 YOLO class 映射与 data.yaml")
    parser.add_argument(
        "--skus", default=None,
        help="只保留这些 sku_id（逗号分隔），用于子集训练。例：--skus SKU010,SKU027,SKU011,SKU025",
    )
    args = parser.parse_args()

    sku_filter = {s.strip() for s in args.skus.split(",")} if args.skus else None
    class_map = build_class_map(sku_filter)
    sku_to_class = {c["sku_id"]: c for c in class_map}

    cm_path = write_class_map(class_map)
    dy_path = write_data_yaml(class_map)

    print(f"商品总数: {len(class_map)}")
    print(f"已生成 class_map.json -> {cm_path}")
    print(f"已生成 data.yaml      -> {dy_path}")
    print()
    print("相似组（class_id 形式，供级联仲裁降级用）:")
    for group in SIMILAR_GROUPS:
        ids = [sku_to_class[s]["class_id"] for s in group if s in sku_to_class]
        names = [s for s in group if s in sku_to_class]
        print(f"  {names} -> class_id {ids}")


if __name__ == "__main__":
    main()
