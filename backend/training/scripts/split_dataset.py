#!/usr/bin/env python3
"""把 raw/ 下的原始图（+标注）划分成 train / val / test。

两条任务线：
  detect   —— 检测：要求每张图有同名 .txt 标注，图片+标注成对搬运
  classify —— 分类：只要图片，按 sku_id 建子目录（目录名即 class）

按 SKU 分层抽样：每个 sku 独立 shuffle 后再按比例切，保证每个 SKU 在
train/val/test 里都有出现（否则像「乐事 40g」这种小 SKU 可能全被划进训练集，
验证集里一次都不出现，评估就没意义）。

用法（在 backend/ 目录下）：
    python training/scripts/split_dataset.py --task detect --train 0.8 --val 0.1 --test 0.1
    python training/scripts/split_dataset.py --task classify --train 0.8 --val 0.2
"""

from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
TRAINING_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = TRAINING_DIR / "datasets" / "raw"
DETECT_DIR = TRAINING_DIR / "datasets" / "detect"
CLASSIFY_DIR = TRAINING_DIR / "datasets" / "classify"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def list_sku_images() -> dict[str, list[Path]]:
    """扫描 raw/<sku_id>/ 下的图片，返回 {sku_id: [图片路径]}。"""
    result: dict[str, list[Path]] = {}
    if not RAW_DIR.exists():
        return result
    for sku_dir in sorted(p for p in RAW_DIR.iterdir() if p.is_dir()):
        imgs = [f for f in sku_dir.iterdir() if f.suffix.lower() in IMAGE_EXTS]
        if imgs:
            result[sku_dir.name] = imgs
    return result


def stratified_split(items: list, train: float, val: float, test: float, seed: int):
    """对单个列表做确定性切分，返回 (train, val, test)。

    用 int() 向下取整分配，余数自动归 train，保证总和精确等于 n；
    当 val/test 比例 >0 但取整后为 0 时保底 1 张，避免小数据量下验证集为空。
    """
    rng = random.Random(seed)
    shuffled = items[:]
    rng.shuffle(shuffled)
    n = len(shuffled)

    n_test = int(n * test)
    n_val = int(n * val)
    n_train = n - n_test - n_val

    if test > 0 and n_test == 0 and n >= 3:
        n_test, n_train = 1, n_train - 1
    if val > 0 and n_val == 0 and (n - n_test) >= 2:
        n_val, n_train = 1, n_train - 1

    return shuffled[:n_train], shuffled[n_train:n_train + n_val], shuffled[n_train + n_val:]


def build_detect(sku_images: dict[str, list[Path]], ratios: dict, seed: int) -> int:
    """图片+标注成对搬运到 detect/{images,labels}/{split}/。返回搬运图片数。"""
    moved = 0
    for sku_id, imgs in sku_images.items():
        # 只保留有对应标注的图
        paired = [p for p in imgs if p.with_suffix(".txt").exists()]
        if not paired:
            print(f"  [跳过] {sku_id}: 无带 .txt 标注的图片（检测任务必须成对）")
            continue
        train, val, test = stratified_split(paired, ratios["train"], ratios["val"], ratios["test"], seed)
        for split, subset in (("train", train), ("val", val), ("test", test)):
            for img in subset:
                out_img = DETECT_DIR / "images" / split / f"{sku_id}_{img.name}"
                out_lbl = DETECT_DIR / "labels" / split / f"{sku_id}_{img.stem}.txt"
                out_img.parent.mkdir(parents=True, exist_ok=True)
                out_lbl.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(img, out_img)
                shutil.copy2(img.with_suffix(".txt"), out_lbl)
                moved += 1
    return moved


def build_classify(sku_images: dict[str, list[Path]], ratios: dict, seed: int) -> int:
    """图片按 sku 子目录搬运到 classify/{split}/{sku_id}/。返回搬运图片数。"""
    moved = 0
    for sku_id, imgs in sku_images.items():
        train, val, test = stratified_split(imgs, ratios["train"], ratios["val"], ratios["test"], seed)
        for split, subset in (("train", train), ("val", val), ("test", test)):
            out_dir = CLASSIFY_DIR / split / sku_id
            out_dir.mkdir(parents=True, exist_ok=True)
            for img in subset:
                shutil.copy2(img, out_dir / f"{sku_id}_{img.name}")
                moved += 1
    return moved


def main() -> None:
    parser = argparse.ArgumentParser(description="划分 raw/ 数据为 train/val/test")
    parser.add_argument("--task", choices=["detect", "classify"], default="detect")
    parser.add_argument("--train", type=float, default=0.8)
    parser.add_argument("--val", type=float, default=0.1)
    parser.add_argument("--test", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    total = args.train + args.val + args.test
    if abs(total - 1.0) > 1e-6:
        raise SystemExit(f"train/val/test 比例之和必须为 1，当前 {total}")
    if min(args.train, args.val, args.test) < 0:
        raise SystemExit("比例不能为负")

    sku_images = list_sku_images()
    if not sku_images:
        raise SystemExit(f"raw/ 下没有图片。请先自采数据放到 {RAW_DIR}/<sku_id>/ 再划分。")

    ratios = {"train": args.train, "val": args.val, "test": args.test}
    if args.task == "detect":
        moved = build_detect(sku_images, ratios, args.seed)
        print(f"检测任务划分完成，共搬运 {moved} 张图 -> {DETECT_DIR}")
    else:
        moved = build_classify(sku_images, ratios, args.seed)
        print(f"分类任务划分完成，共搬运 {moved} 张图 -> {CLASSIFY_DIR}")

    print(f"覆盖 SKU 数: {len(sku_images)}")


if __name__ == "__main__":
    main()
