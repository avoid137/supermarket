# YOLO 训练工作区

给「视觉结账」训练一个本地 YOLO 模型，补齐现有 Qwen-VL 链路的两块短板：**真实 bbox 定位**（现在是占位框）和**细粒度规格区分**（乐事 40g vs 70g、奥利奥 97g 盒 vs 116g 卷）。

## 目录结构

```
training/
├── data.yaml              # 检测任务配置（make_catalog.py 生成，勿手改）
├── class_map.json         # class_id <-> sku_id 双向映射（推理接入层用）
├── train.py               # 训练入口（检测 / 分类两条路线）
├── export.py              # best.pt -> ONNX
├── requirements.txt       # 训练依赖（ultralytics）
├── scripts/
│   ├── make_catalog.py       # 从 products.py 生成 class_map.json + data.yaml
│   ├── convert_annotations.py# VOC XML / labelme JSON -> YOLO txt
│   ├── split_dataset.py      # raw/ -> detect/ 或 classify/（分层抽样）
│   └── collect_shots.py      # 自采导入 + 难例落盘
└── datasets/
    ├── raw/               # 原始自采图（按 sku_id 分子目录，唯一需要你手动填的地方）
    ├── detect/            # 检测任务：images/ + labels/ 各分 train/val/test
    └── classify/          # 分类任务：train/val/test 下按 sku_id 分子目录
```

## class 映射（核心约定）

- **唯一事实来源是 `app/data/products.py`**。加商品后重跑 `make_catalog.py`，映射自动更新，不会漂移。
- class_id 按 sku_id 排序分配：`SKU001 -> 0`、`SKU002 -> 1` … `SKU028 -> 27`。
- 检测任务的 `names` 直接用 `sku_id`，所以推理时 `class_id` 即 `sku_id`，无需再查表。
- `class_map.json` 里同时带了 `similar_groups`（转成 class_id 形式），供将来级联仲裁层判断「YOLO 结果落进相似组 → 降级交大模型」。

## 快速开始

```bash
# 0) 装依赖（先激活本项目虚拟环境，下面的 python 均指该环境的解释器）
python -m pip install -r training/requirements.txt

# 1) 生成 class 映射（改过商品后也重跑这一步）
python training/scripts/make_catalog.py
```

## 路线 A：分类（先验证「40g vs 70g 能否分开」，推荐先做）

1. 自采图，每款 30~50 张，放到 `datasets/raw/<sku_id>/`（如 `raw/SKU027/`）。
2. 划分（分类任务不需要标注框）：

```bash
python training/scripts/split_dataset.py --task classify --train 0.8 --val 0.2
```

3. 训练，跑完看混淆矩阵里 `SKU027(40g) ↔ SKU010(70g)` 这一格：

```bash
python training/train.py --task classify --epochs 50
```

## 路线 B：检测（解决「占位框」，需要标框）

1. 自采图放 `datasets/raw/<sku_id>/`。
2. 用 labelImg（VOC XML）或 labelme（JSON）标框，**label 必须是 sku_id**。
3. 转 YOLO 格式：

```bash
python training/scripts/convert_annotations.py --input training/datasets/raw
```

4. 划分（图片 + 标注成对搬运）：

```bash
python training/scripts/split_dataset.py --task detect --train 0.8 --val 0.1 --test 0.1
```

5. 训练（微调：先冻结骨干 10 层训头，再解冻精调可提升小样本效果）：

```bash
python training/train.py --task detect --model yolov8n.pt --epochs 100 --freeze 10
```

6. 导出 ONNX：

```bash
python training/export.py --weights training/runs/detect/weights/best.pt
```

## YOLO 标注格式（一个示例）

每张图一个同名 `.txt`，每行 `class_id cx cy w h`，坐标归一化到 0~1：

```
# SKU027(乐事40g) 的标注，class_id=26
26 0.500000 0.520000 0.180000 0.400000
```

## 预训练基座（可选，能减少自采量）

先下载公开数据集做预训练，再微调，可显著降低自采需求：

| 数据集 | 用途 | 说明 |
|---|---|---|
| RP2K | 分类特征预训练 | 2000+ SKU 中国货架，最可能覆盖你的中国品牌 |
| RPC | 检测预训练 | 200 SKU 中国收银结算，含检测框+类别 |
| SKU-110K | 通用定位预训练 | 单类「object」，173 万框，只学定位不分分类 |

注意：公开数据集只解决「通用定位/特征」，**细粒度规格区分（40g vs 70g）只能靠自采数据微调**。

## 难例落盘（数据飞轮）

结账流程里「识别失败 + 人工确认」的样本是最有价值的训练数据。`scripts/collect_shots.py` 提供了复用函数 `save_hard_example(src, sku_id)`，供 checkout 流程调用；也可手动导入：

```bash
python training/scripts/collect_shots.py import --src ./某目录 --sku SKU027 --hard
```

## 尚未接入的部分

本工作区只覆盖「训练准备 + 训练」。训练出 `best.pt` / ONNX 后，还需写 `backend/app/services/detectors/yolo.py` 把它接进现有 `recognize()` 链路（输出 `Detection` 喂给 `arbitration.py`，对外接口不变）。需要时可继续。
