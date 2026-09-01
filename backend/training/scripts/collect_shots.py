#!/usr/bin/env python3
"""自采数据归集 + 难例自动落盘（数据飞轮）。

两件事：
  1. 批量导入：把一批已经按 SKU 分好的照片导入 raw/<sku_id>/
  2. 难例落盘：结账流程里识别失败、经人工确认的商品图，自动存进 raw/<sku_id>/hard/
     —— 这些是「模型最容易看错」的样本，攒几个月就是最贴合本场景的训练数据。

``save_hard_example`` 是给 checkout 流程调用的复用函数，别在 CLI 里重复实现。

用法（在 backend/ 目录下）：
    python training/scripts/collect_shots.py import --src ./photos/SKU001 --sku SKU001
    python training/scripts/collect_shots.py import --src ./photos/SKU001 --sku SKU001 --hard
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

TRAINING_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = TRAINING_DIR / "datasets" / "raw"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def save_hard_example(src: Path, sku_id: str, dest_dir: Path | None = None) -> Path:
    """把一张难例图落盘到 raw/<sku_id>/hard/，文件名带时间戳避免覆盖。

    供 checkout / arbitration 在「识别失败 + 人工确认 sku」后调用。
    """
    root = dest_dir or RAW_DIR
    out_dir = root / sku_id / "hard"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{stamp}_{src.name}"
    shutil.copy2(src, out_path)
    return out_path


def import_dir(src: Path, sku_id: str, hard: bool = False) -> int:
    """把 src 目录下的图片导入 raw/<sku_id>/（hard=True 时进 hard 子目录）。"""
    if not src.is_dir():
        raise SystemExit(f"源路径不是目录: {src}")
    out_dir = RAW_DIR / sku_id / ("hard" if hard else "")
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for f in sorted(src.iterdir()):
        if f.suffix.lower() not in IMAGE_EXTS:
            continue
        shutil.copy2(f, out_dir / f.name)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="自采数据归集 / 难例落盘")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import", help="导入一个目录的图片到 raw/<sku_id>/")
    p_import.add_argument("--src", required=True, help="图片目录")
    p_import.add_argument("--sku", required=True, help="sku_id，如 SKU001")
    p_import.add_argument("--hard", action="store_true", help="作为难例存入 hard/ 子目录")

    args = parser.parse_args()
    if args.cmd == "import":
        n = import_dir(Path(args.src).resolve(), args.sku, args.hard)
        sub = "hard/" if args.hard else ""
        print(f"已导入 {n} 张图 -> {RAW_DIR / args.sku / sub}")


if __name__ == "__main__":
    main()
