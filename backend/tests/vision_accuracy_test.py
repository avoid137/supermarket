"""视觉识别准确率回归测试。

不依赖真实图片和网络：直接把各种模型输出喂给 build_detections，
验证闭卷选择、未知 SKU 丢弃、置信度校准与三态仲裁是否按预期工作。

运行：python tests/vision_accuracy_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.database import init_db
from app.repositories import product_repo
from app.services.vision import build_catalog, build_vision_prompt, build_detections

init_db(seed_if_empty=True)
settings = get_settings()

products = product_repo.list_products()
by_sku = {p.sku_id: p for p in products}
boxes = [(10.0, 30.0, 18.0, 20.0)] * 6

failures = 0


def check(name: str, detections, expect_state: str, expect_sku: str | None = None):
    global failures
    d = detections[0] if detections else None
    if d is None:
        print(f"  ✗ {name}：没有任何检测结果")
        failures += 1
        return
    ok = d.state == expect_state
    sku_ok = True
    if expect_sku is not None:
        sku_ok = bool(d.candidates) and d.candidates[0].sku_id == expect_sku
    top = f"{d.candidates[0].sku_id} {d.candidates[0].score:.2f}" if d.candidates else "（无候选）"
    mark = "✓" if (ok and sku_ok) else "✗"
    if not (ok and sku_ok):
        failures += 1
    print(f"  {mark} {name}：state={d.state}（期望 {expect_state}）top={top}")


print("=" * 72)
print("一、闭卷选择清单（前 6 行 / 共 %d 行）" % len(products))
print("=" * 72)
for line in build_catalog(products).split("\n")[:6]:
    print("  " + line)

print()
print("=" * 72)
print("二、模型输出 → 判定结果")
print("=" * 72)

cases = [
    (
        "压倒性优势 → 自动入账",
        [{"candidates": [{"sku_id": "SKU001", "confidence": 0.97},
                         {"sku_id": "SKU002", "confidence": 0.02}], "quantity": 1}],
        "auto", "SKU001",
    ),
    (
        "相似包装难分（白桃/葡萄）→ 追问顾客",
        [{"candidates": [{"sku_id": "SKU003", "confidence": 0.85},
                         {"sku_id": "SKU004", "confidence": 0.72}], "quantity": 1}],
        "clarify", "SKU003",
    ),
    (
        "置信度整体偏低 → 转人工复核",
        [{"candidates": [{"sku_id": "SKU009", "confidence": 0.50},
                         {"sku_id": "SKU010", "confidence": 0.45}], "quantity": 1}],
        "review", "SKU009",
    ),
    (
        "唯一候选且分数够 → 自动入账（不打扰顾客）",
        [{"candidates": [{"sku_id": "SKU007", "confidence": 0.93}], "quantity": 2}],
        "auto", "SKU007",
    ),
    (
        "模型编造清单外 sku_id → 记为未识别，不得凭空入账",
        [{"candidates": [{"sku_id": "SKU999", "confidence": 0.99}], "quantity": 1}],
        "review", None,
    ),
    (
        "模型明确表示看不清 → 记为未识别",
        [{"candidates": [], "quantity": 1}],
        "review", None,
    ),
    (
        "部分候选无效时保留有效候选",
        [{"candidates": [{"sku_id": "SKU999", "confidence": 0.99},
                         {"sku_id": "SKU005", "confidence": 0.90}], "quantity": 1}],
        "auto", "SKU005",
    ),
]

for name, raw_items, expect_state, expect_sku in cases:
    check(name, build_detections(raw_items, by_sku, boxes, settings), expect_state, expect_sku)

print()
print("=" * 72)
print("三、关键性质")
print("=" * 72)

unrecognized = build_detections([{"candidates": [], "quantity": 1}], by_sku, boxes, settings)[0]
print(f"  未识别项候选数：{len(unrecognized.candidates)}（必须为 0）")
print(f"  未识别项提示语：{unrecognized.message}")
if len(unrecognized.candidates) != 0:
    failures += 1

tie = build_detections(
    [{"candidates": [{"sku_id": "SKU003", "confidence": 0.90},
                     {"sku_id": "SKU004", "confidence": 0.88}], "quantity": 1}],
    by_sku, boxes, settings,
)[0]
solo = build_detections(
    [{"candidates": [{"sku_id": "SKU003", "confidence": 0.90}]}], by_sku, boxes, settings,
)[0]
print(f"  并驾齐驱(0.90/0.88) 折算分：{tie.candidates[0].score:.3f} → {tie.state}")
print(f"  独占鳌头(0.90 无对手) 折算分：{solo.candidates[0].score:.3f} → {solo.state}")
if tie.candidates[0].score >= solo.candidates[0].score:
    print("  ✗ 置信度校准失效：难分伯仲时分数没有被下调")
    failures += 1
else:
    print("  ✓ 置信度校准生效：难分伯仲时分数被下调")

print()
print("=" * 72)
print(f"失败 {failures} 项" if failures else "全部通过")
print("=" * 72)
sys.exit(1 if failures else 0)
