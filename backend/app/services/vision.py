"""视觉识别适配层：模拟引擎与真实多模态模型自动切换。

- 有 VISION_API_KEY 且 use_vision_model=True → 调用 Qwen-VL（OpenAI 兼容协议）
- 否则 → 走内置示例场景的模拟引擎，保证无 Key 也能完整演示
- 真实模型调用失败 → 自动降级到模拟引擎，不阻断结账流程

准确率的关键不在模型有多大，而在这一层的三个设计约束：

1. 闭卷选择：把 SKU 清单交给模型，让它输出 sku_id 而不是自由编商品名。
   开放生成会产生「矿泉水」「洗发水」这类泛化词，再交给模糊检索去猜，
   必然出现「洗发水 → 可口可乐」这种错配，且系统永远不觉得自己错了。

2. 允许说不知道：清单外的 sku_id 一律丢弃，全部候选都无效时如实记为未识别，
   转人工复核，而不是强行塞一个商品进账单。

3. 置信度校准：模型自报的分数普遍虚高，用「第一名领先第二名多少」来折扣，
   并驾齐驱时宁可追问顾客，也不替顾客做决定。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter

import httpx

from app.core.config import Settings
from app.data.products import SIMILAR_GROUPS
from app.data.scenes import SCENE_MAP, SCENES
from app.models.schemas import Candidate, Detection, Product
from app.prompts import render_vision_prompt
from app.repositories import product_repo
from app.services.arbitration import build_message, decide_state

# 包装形态翻译成中文，帮助模型把「罐装 / 瓶装 / 袋装」与画面特征对上
SHAPE_CN = {
    "can": "罐装",
    "bottle": "瓶装",
    "box": "盒装",
    "bag": "袋装",
    "tube": "管装",
    "pack": "包装",
    "cup": "杯装",
}

# 与头名分差在此范围之内的候选，视为真正可能造成混淆的对手。
# 分差更大的候选没有追问价值——模型已经把它们排除了。
COMPETITOR_GAP = 0.25

# 尺子比例尺探测提示词（演示用）。
# 让视觉大模型读取画面里的尺子刻度，只回报「每厘米对应多少像素」与是否有尺子。
# 不涉及商品判断——模型擅长看图读数字，把这件它擅长的事单独拆出来，
# 比让它顺带在商品识别里报尺寸要可靠得多。
RULER_PROBE_PROMPT = (
    "请观察画面中是否有一把带刻度的尺子。\n"
    "如果有：读取尺子刻度，推算「1 厘米在画面里对应多少像素」，即 px_per_cm。\n"
    "只输出 JSON，不要任何解释：{\"has_ruler\": true, \"px_per_cm\": 数值}\n"
    "如果没有尺子：输出 {\"has_ruler\": false, \"px_per_cm\": 0}\n"
    "注意：px_per_cm 必须是数字（每厘米对应的像素数），不要输出尺子总长度。"
)

_mock_round_robin = 0

# 最近一次真实视觉模型调用的报错。
# 健康检查只判断「密钥是否非空」会骗人：鉴权被拒时它照样报 vision_enabled=true，
# 故障被一路掩盖到顾客眼前，表现为「识别结果莫名其妙」。
_last_vision_error: str | None = None

logger = logging.getLogger(__name__)


def get_vision_error() -> str | None:
    return _last_vision_error


def _set_vision_error(message: str | None) -> None:
    global _last_vision_error
    _last_vision_error = message


def _next_scene_id() -> str:
    """未指定场景时轮换三个示例场景，让摄像头拍照也能看到不同结果。"""
    global _mock_round_robin
    scene_id = SCENES[_mock_round_robin % len(SCENES)].scene_id
    _mock_round_robin += 1
    return scene_id


def _placeholder_boxes(count: int) -> list[tuple[float, float, float, float]]:
    """真实模型无法回传精确坐标时，在盘面均匀铺开占位框供前端展示。"""
    if count <= 0:
        return []
    per_row = 4 if count > 4 else count
    rows = (count + per_row - 1) // per_row
    boxes = []
    for i in range(count):
        row, col = divmod(i, per_row)
        n_in_row = min(per_row, count - row * per_row)
        w = 88.0 / n_in_row
        x = 6.0 + col * w + 1.0
        h = 22.0 if rows == 1 else (44.0 / rows)
        y = 28.0 + row * (46.0 / rows)
        boxes.append((x, y, w - 2.0, h - 2.0))
    return boxes


def build_catalog(products: list[Product]) -> str:
    """把商品主数据压成模型能一眼扫完的候选清单。

    带上规格、包装形态和视觉特征，是因为同品牌不同口味（乐事黄瓜味 / 原味）
    在外观上只差这一点信息，不给模型等于逼它猜。
    """
    lines = []
    for p in products:
        shape = SHAPE_CN.get(p.visual.shape, p.visual.shape)
        lines.append(f"- {p.sku_id} {p.name}（{p.spec}，{shape}，外观特征：{p.visual.label}）")
    return "\n".join(lines)


def build_vision_prompt(catalog: str, cropped: bool = False) -> str:
    """构建闭卷选择提示词。

    文案已抽到 app/prompts/checkout.yaml，本函数只负责取用。

    改 YAML 时请一并遵守这两条设计意图：

    - 三步法里的 OCR 环节，是因为清单中存在「同名同口味、只有规格不同」的商品
      （乐事 40g / 70g、奥利奥 97g / 116g）。这类商品在整图里的外观差异几乎看不出来，
      但包装上印着的净含量是确定性的区分依据——读字比看图可靠。
    - cropped=True 用于级联模式：画面是 YOLO 裁出来的单个商品，视野更近、文字更清晰，
      但也可能被裁掉一部分。因此 YAML 的 extra.cropped 里配了一条与「鼓励判断」
      同等强度的否决规则：明显不是商品就返回空数组。检测器不认识类别，
      框里可能根本不是商品，认错了就是让顾客为不存在的东西买单。

    保留本函数而不让调用方直接用 app.prompts，是因为三个测试脚本
    （vision_accuracy_test / new_sku_test / vision_live_probe）都从这里导入。
    """
    return render_vision_prompt(catalog, cropped=cropped)


def calibrate_confidence(top_conf: float, gap: float) -> float:
    """把模型自报的置信度折算成可用于仲裁的分数。

    大模型自报分数普遍虚高，而「第一名领先第二名多少」比绝对分数更能说明问题：
    两个候选并驾齐驱时，即便自报 0.9 也不该自动入账。
    """
    top_conf = min(max(top_conf, 0.0), 1.0)
    if gap >= 0.50:
        factor = 1.0  # 压倒性优势，基本采信
    elif gap >= 0.30:
        factor = 0.95
    elif gap >= 0.15:
        factor = 0.85
    else:
        factor = 0.70  # 难分伯仲，明显下调后交给追问机制
    return round(top_conf * factor, 3)


def recognize_mock(scene_id: str | None, settings: Settings) -> tuple[list[Detection], str]:
    scene = SCENE_MAP.get(scene_id or "") or SCENE_MAP[_next_scene_id()]
    detections: list[Detection] = []

    for idx, item in enumerate(scene.items):
        product = product_repo.get_product(item.sku_id)
        if product is None:
            continue
        candidates = [Candidate(sku_id=item.sku_id, name=product.name, score=round(item.base_score, 3))]
        for other_sku in item.ambiguous_with:
            other = product_repo.get_product(other_sku)
            if other:
                # 相似候选分数贴近主候选，差距小才会触发追问而不是误判
                candidates.append(
                    Candidate(sku_id=other_sku, name=other.name, score=round(item.base_score - 0.05, 3))
                )
        candidates.sort(key=lambda c: c.score, reverse=True)

        state = decide_state(candidates[0].score, settings, has_competitors=len(candidates) > 1)
        detections.append(
            Detection(
                box_id=f"{scene.scene_id}-{idx + 1}",
                x=item.x,
                y=item.y,
                w=item.w,
                h=item.h,
                candidates=candidates,
                quantity=item.quantity,
                state=state,
                message=build_message(state, candidates, item.occluded),
            )
        )

    return detections, scene.scene_id


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("模型未返回可解析的 JSON")
    return json.loads(match.group(0))


def _extract_grams(spec: str) -> int | None:
    """从规格字符串里提取净含量克数，如 '70g 袋装' -> 70、'净含量:70克' -> 70。拿不到返回 None。"""
    if not spec:
        return None
    # 匹配 "70g"、"70G"、"70 克"、"70克"、"净含量:70g" 等格式
    m = re.search(r"(\d+)\s*(?:g|G|克)\b", spec)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _pick_sku_in_group(
    gram: int,
    group_skus: tuple[str, ...],
    products_by_sku: dict[str, Product],
    preferred_name: str | None = None,
) -> str | None:
    """在相似组里挑一个规格为 gram 的 SKU。

    - 优先挑与 preferred_name 同名的（保留视觉判定的风味，如「原味」），
      避免把 70g 原味强行翻成 70g 黄瓜。
    - 同 gram 可能对应多个 SKU（SKU009/010 都是 70g），这里只挑一个，
      具体选哪个不影响价格（同 gram SKU 定价一致）。
    拿不到返回 None。
    """
    cands = [
        s for s in group_skus
        if s in products_by_sku and _extract_grams(products_by_sku[s].spec) == gram
    ]
    if not cands:
        return None
    if preferred_name:
        for s in cands:
            if products_by_sku[s].name == preferred_name:
                return s
    return cands[0]


def _reassign_detection_sku(
    det: Detection,
    target_sku: str,
    products_by_sku: dict[str, Product],
    reason: str,
) -> None:
    """把检测结果的头名候选换成 target_sku，并同步 state/evidence/message。

    这是修复「尺寸排序/排除法只改 evidence 文字、不改 sku_id」这一 bug 的核心：
    之前 detection.evidence 写着「推断40g」，但 det.candidates 仍是原来的 70g 候选，
    confirm/pay 读 candidates.sku_id 仍按 70g 入账——文字与账单不一致。
    """
    top_score = det.candidates[0].score if det.candidates else 0.9
    new_cands = [Candidate(sku_id=target_sku, name=products_by_sku[target_sku].name, score=top_score)]
    seen = {target_sku}
    for c in det.candidates:
        if c.sku_id in seen:
            continue
        seen.add(c.sku_id)
        new_cands.append(c)
    det.candidates = new_cands
    det.state = "auto"
    det.evidence = reason
    det.message = build_message("auto", det.candidates)


def _spec_siblings(product: Product, products_by_sku: dict[str, Product]) -> list[Product]:
    """返回库中与 product「同名但不同规格」的兄弟款（如乐事黄瓜味 70g ↔ 40g）。

    这类商品靠外观无法区分规格，必须借助 OCR 净含量或尺子比例尺等第二信号，
    否则不得自动入账。

    注意与 SIMILAR_GROUPS（易混包装组，如可口可乐 / 百事可乐）区分：
    后者是**不同商品**、只是包装相似，标签文字本身足以区分，
    视觉置信度分差就是有效证据，不应强制转人工复核。
    """
    if product is None:
        return []
    return [
        other
        for other in products_by_sku.values()
        if other.sku_id != product.sku_id
        and other.name == product.name
        and other.spec != product.spec
    ]


def _resolve_similar_group(
    top_sku: str,
    meas: float | None,
    ocr_text: str,
    products_by_sku: dict[str, Product],
) -> tuple[str, str | None]:
    """在同款不同规格的相似组里，用 OCR 净含量 + 尺子实测长度选出最贴合的 SKU。

    返回 (最终 sku, 纠偏原因)。原因用于日志与界面提示；未触发纠偏时返回 (原 sku, None)。

    设计要点：
      - 先在同「名称（即同风味）」的兄弟款里找，70g 与 40g 同风味时 name 完全相同，
        这样尺子只换规格、不串风味（黄瓜 70g 不会误成原味 40g）。
      - OCR 净含量优先：包装印刷体数字比看外观可靠，命中且与视觉判定克数不同即切换。
      - 尺子实测长度兜底：仅当 top 自身有尺寸标定、且实测值明显更贴近某兄弟款时才切换
        （差值小 0.1cm 以上），以过滤尺子读数噪声、避免误翻转。
    """
    # 诊断日志：打印入参，定位「尺子/OCR 纠偏为何不触发」
    logger.info(
        "[DIAG] _resolve_similar_group 入参: top_sku=%r meas=%r ocr_text=%r",
        top_sku, meas, ocr_text,
    )

    group = next((g for g in SIMILAR_GROUPS if top_sku in g), None)
    if not group:
        logger.info("[DIAG] SKU %r 不在任何 SIMILAR_GROUPS 中，跳过纠偏", top_sku)
        return top_sku, None
    members = [products_by_sku[s] for s in group if s in products_by_sku]
    top = products_by_sku.get(top_sku)
    if not top or not members:
        return top_sku, None

    ocr = (ocr_text or "").lower()
    # 同风味兄弟款优先作为候选池，避免串风味
    same_name = [m for m in members if m.sku_id != top_sku and m.name == top.name]
    pool = same_name if same_name else members

    logger.info(
        "[DIAG] 纠偏候选池: top_name=%r pool_skus=%s same_name=%s",
        top.name, [m.sku_id for m in pool], [m.sku_id for m in same_name],
    )

    # 1) OCR 净含量：命中组内某款规格克数、且与视觉判定克数不同 -> 切换
    #    匹配 "70g"、"70G"、"70 克"、"70克"、"净含量:70g" 等格式
    top_w = _extract_grams(top.spec)
    if ocr:
        for m in pool:
            w = _extract_grams(m.spec)
            if w and w != top_w:
                pattern = rf"{w}\s*(?:g|G|克)\b"
                if re.search(pattern, ocr):
                    logger.info("[DIAG] OCR纠偏命中: ocr含'%d克/g' -> 切换 %s => %s", w, top_sku, m.sku_id)
                    return m.sku_id, f"OCR净含量{w}g"
        logger.info("[DIAG] OCR未命中: top_w=%r ocr=%r (无匹配的规格g/克数)", top_w, ocr[:80] if ocr else "")

    # 2) 尺子实测长度兜底
    top_pkg = getattr(top.visual, "pkg_length_cm", 0.0) or 0.0
    # 合理性下限：YOLO 框经常只覆盖包装中心图案（尤其对软包装薯片袋），
    # 导致实测长度远小于真实外尺寸（常见偏差 -40%~-65%）。
    # 若实测值低于标定值的 60%，视为「框不完整，数据不可信」，跳过尺子纠偏，
    # 避免用错误的短长度把 70g 错翻成 40g。
    MIN_LENGTH_RATIO = 0.60
    if meas and meas > 0 and top_pkg > 0 and meas >= top_pkg * MIN_LENGTH_RATIO:
        top_diff = abs(top_pkg - meas)
        best, best_diff = top_sku, top_diff
        for m in pool:
            pkg = getattr(m.visual, "pkg_length_cm", 0.0) or 0.0
            if pkg <= 0:
                continue
            d = abs(pkg - meas)
            if d + 0.1 < best_diff:
                best, best_diff = m.sku_id, d
        logger.info(
            "[DIAG] 尺子纠偏: meas=%.1fcm top_pkg=%.1fcm(diff=%.1f) best=%s(best_diff=%.1f) 切换=%s",
            meas, top_pkg, top_diff, best, best_diff, best != top_sku,
        )
        if best != top_sku:
            return best, f"尺子实测{meas:.1f}cm"
    elif meas and top_pkg > 0:
        logger.info(
            "[DIAG] 尺子纠偏跳过: meas=%.1fcm < top_pkg=%.1fcm×%.0f%% (框不完整，数据不可信)",
            meas, top_pkg, MIN_LENGTH_RATIO * 100,
        )
    return top_sku, None


def _resolve_by_elimination(detections: list[Detection], products_by_sku: dict[str, Product]) -> None:
    """相似组排除法：当同组商品有多件、且部分已 OCR 确认规格时，
    未确认的实例通过「剩余规格唯一」自动分配。

    适用场景：一帧里同时出现 70g 和 40g 两袋乐事，大袋 OCR 读到
    「净含量:70克」→ 自动确认为 70g，小袋 OCR 未读到 → 通过排除法
    推断为 40g（因为组内只剩这一个未分配的规格）。

    安全约束：
    - 仅当未确认实例数 ≤ 组内剩余规格数时才触发（避免多件同规格时错分）
    - 仅修改 state=review/clarify 且 evidence 含「规格需人工确认」的实例
    - 未确认实例必须通过质量过滤（置信度 ≥ 阈值），排除 YOLO 产生的杂框
    """
    # 按相似组聚合
    from collections import defaultdict
    group_dets: dict[str, list[Detection]] = defaultdict(list)
    for d in detections:
        top = d.candidates[0] if d.candidates else None
        if not top:
            continue
        prod = products_by_sku.get(top.sku_id)
        if not prod:
            continue
        for g in SIMILAR_GROUPS:
            if prod.sku_id in g:
                group_dets[tuple(g)].append(d)
                break

    for group_skus, dets in group_dets.items():
        if len(dets) < 2:
            continue
        # 收集已确认的规格（OCR 确认或 auto 入账）
        confirmed_grams: set[int] = set()
        unconfirmed: list[Detection] = []
        for d in dets:
            top_cand = d.candidates[0] if d.candidates else None
            if not top_cand:
                continue
            prod = products_by_sku.get(top_cand.sku_id)
            if not prod:
                continue
            ocr_g = _extract_grams(d.evidence or "")
            visual_g = _extract_grams(prod.spec)
            # 已确认：state=auto 且 OCR 视觉一致，或者 evidence 明确含规格数字
            if d.state == "auto" and ocr_g is not None and ocr_g == visual_g:
                confirmed_grams.add(ocr_g)
            elif ("规格需人工确认" in (d.evidence or "")) and d.state in ("review", "clarify") and top_cand.score >= 0.5:
                unconfirmed.append(d)

        if not confirmed_grams or not unconfirmed:
            continue
        # 组内所有 SKU 的规格集合
        all_group_grams = set()
        for sku in group_skus:
            p = products_by_sku.get(sku)
            if p:
                g = _extract_grams(p.spec)
                if g is not None:
                    all_group_grams.add(g)
        remaining = all_group_grams - confirmed_grams
        # 安全：只有当剩余规格数 == 未确认实例数（一一对应）时才分配
        if len(remaining) == len(unconfirmed):
            for det, gram in zip(unconfirmed, sorted(remaining)):
                old_state = det.state
                target = _pick_sku_in_group(
                    gram, group_skus, products_by_sku,
                    preferred_name=det.candidates[0].name if det.candidates else None,
                )
                if not target:
                    continue
                reason = f"排除法推断{gram}g（同帧{', '.join(f'{g}g' for g in sorted(confirmed_grams))}已OCR确认）"
                _reassign_detection_sku(det, target, products_by_sku, reason)
                logger.info(
                    "[DIAG] 排除法成功: %s %s→auto %s 推断%dg (已确认%s)",
                    det.box_id, old_state, target, gram,
                    ",".join(f"{g}g" for g in sorted(confirmed_grams)),
                )


def _resolve_by_size_ranking(detections: list[Detection], products_by_sku: dict[str, Product]) -> None:
    """相似组尺寸排序：用尺子实测长度的相对排序区分同款不同规格。

    当一帧内出现多件相似组商品（如 70g + 40g 两袋乐事）且 OCR 都没读到
    净含量时，利用「大袋实测 > 小袋实测」这一可靠的相对关系，
    按实测长度降序匹配组内 SKU 标定长度降序——大袋配大规格、小袋配小规格。

    前提条件（全部满足才触发）：
    - 同组 ≥ 2 件检测且都有有效实测长度
    - 实测长度之间有足够差距（>10%，排除测量噪声）
    - 检测件数 ≤ 组内未分配规格数（避免多件同规格时错分）
    - 仅处理 state=review/clarify 且含「规格需人工确认」的实例
    - 实测长度超过最小阈值（排除 YOLO 杂框）
    """
    from collections import defaultdict

    group_dets: dict[tuple, list[Detection]] = defaultdict(list)
    for d in detections:
        top = d.candidates[0] if d.candidates else None
        if not top:
            continue
        prod = products_by_sku.get(top.sku_id)
        if not prod:
            continue
        for g in SIMILAR_GROUPS:
            if prod.sku_id in g:
                group_dets[tuple(g)].append(d)
                break

    # 同类包装最小可信实测长度（cm）：低于此值的框视为杂框/噪声
    MIN_CREDIBLE_MEAS_CM = 5.0

    for group_skus, dets in group_dets.items():
        if len(dets) < 2:
            continue

        # 先收集本组已确认的规格（OCR 确认或尺寸排序已分配的），后续分配时跳过
        confirmed_grams: set[int] = set()
        for d in dets:
            top_cand = d.candidates[0] if d.candidates else None
            if not top_cand:
                continue
            prod = products_by_sku.get(top_cand.sku_id)
            if not prod:
                continue
            ocr_g = _extract_grams(d.evidence or "")
            visual_g = _extract_grams(prod.spec)
            if d.state == "auto" and ocr_g is not None and ocr_g == visual_g:
                confirmed_grams.add(ocr_g)
            elif "尺寸排序推断" in (d.evidence or "") and ocr_g is not None:
                confirmed_grams.add(ocr_g)

        # 筛选有有效实测长度 + 需要确认 + 质量达标的实例
        valid = [
            d for d in dets
            if getattr(d, "_meas", None) is not None and d._meas >= MIN_CREDIBLE_MEAS_CM
            and d.state in ("review", "clarify")
            and "规格需人工确认" in (d.evidence or "")
            and (d.candidates[0].score >= 0.5 if d.candidates else False)
        ]
        if len(valid) < 1:
            continue

        # 按实测长度降序
        valid.sort(key=lambda d: getattr(d, "_meas", 0) or 0, reverse=True)
        meas_values = [getattr(d, "_meas", 0) or 0 for d in valid]

        # 多实例时要求有足够差距；单实例也允许（配合排除法使用）
        if len(valid) >= 2 and meas_values[0] > 0 and (meas_values[-1] / meas_values[0]) > 0.9:
            logger.info("[DIAG] 尺寸排序跳过: 差距不足 %.0f%%", (1 - meas_values[-1]/meas_values[0])*100)
            continue

        # 组内 SKU 按标定长度降序，去掉已确认的规格（去重：多个 SKU 可能同规格）
        group_skus_sorted = sorted(
            [s for s in group_skus if s in products_by_sku],
            key=lambda s: getattr(products_by_sku[s].visual, "pkg_length_cm", 0) or 0,
            reverse=True,
        )
        seen_grams: set[int] = set()
        available_grams: list[int] = []
        for s in group_skus_sorted:
            g = _extract_grams(products_by_sku[s].spec)
            if g is not None and g not in confirmed_grams and g not in seen_grams:
                available_grams.append(g)
                seen_grams.add(g)

        # 安全：仅当可分配实例数 ≤ 可分配规格数时才匹配
        if len(valid) <= len(available_grams):
            for det, gram in zip(valid, available_grams):
                old_state = det.state
                target = _pick_sku_in_group(
                    gram, group_skus, products_by_sku,
                    preferred_name=det.candidates[0].name if det.candidates else None,
                )
                if not target:
                    continue
                meas_val = getattr(det, "_meas", 0) or 0
                reason = f"尺寸排序推断{gram}g（实测{meas_val:.1f}cm，同帧最大{meas_values[0]:.1f}cm）"
                _reassign_detection_sku(det, target, products_by_sku, reason)
                logger.info(
                    "[DIAG] 尺寸排序成功: %s %s→auto %s 推断%dg (meas=%.1fcm, 排名%d/%d)",
                    det.box_id, old_state, target, gram, meas_val,
                    valid.index(det) + 1, len(valid),
                )


def _group_of_sku(sku_id: str, products_by_sku: dict[str, Product]) -> tuple | None:
    """返回某 SKU 所属的相似组（tuple），不在任何组则返回 None。"""
    if sku_id not in products_by_sku:
        return None
    for g in SIMILAR_GROUPS:
        if sku_id in g:
            return tuple(g)
    return None


def _resolve_by_weight(
    detections: list[Detection],
    tray_weight_g: float | None,
    products_by_sku: dict[str, Product],
    settings: Settings,
) -> None:
    """相似组重量终裁：当 OCR/尺子/尺寸排序都分不清规格时，用整盘总重做组合约束。

    思路：对相似组里仍待确认（state=review/clarify 且 evidence 含「规格需人工确认」）
    的实例，枚举它们在组内各规格上的分配组合，计算该组合的总重量，与
    「整盘实称 − 皮重 − 已确认实例重量」的残差比对；仅当**唯一**一个组合落在
    容差内时才应用，把对应实例的候选切换到该规格。

    优先级（与用户确认的方向一致）：
    - OCR 已确认的实例视为 definite（重量计入 base，不参与重排，也不会被重量覆盖）。
    - 尺寸排序/排除法已分配(auto)的实例同样视为 definite。
    - 重量仅对「仍未确定规格」的相似组实例生效，且只在能唯一确定时才动手。

    安全约束：
    - tray_weight_g 为 None 时整体跳过（无传感器读数）。
    - 存在任何「既非 definite、也非相似组规格待确认」的 review 项时跳过——
      说明托盘上有无法归类的商品，重量无法可靠归因，宁可不自动入账。
    - 至少要有 1 个 definite 或 1 个歧义实例参与才有意义；全空则跳过。
    """
    if tray_weight_g is None:
        return

    tolerance = getattr(settings, "WEIGHT_TOLERANCE_G", 25.0)
    tare = getattr(settings, "TRAY_TARE_G", 0.0)

    definite: list[Detection] = []
    ambiguous: list[Detection] = []   # 相似组规格待确认实例
    other_review: list[Detection] = []  # 无法归类的 review 项

    for d in detections:
        top = d.candidates[0] if d.candidates else None
        if not top:
            # 完全没有候选（真·未识别），重量无法归因
            if d.state == "review":
                other_review.append(d)
            continue
        if top.sku_id not in products_by_sku:
            continue
        is_similar = _group_of_sku(top.sku_id, products_by_sku) is not None
        is_spec_pending = (
            d.state in ("review", "clarify")
            and "规格需人工确认" in (d.evidence or "")
            and (top.score >= 0.5 if hasattr(top, "score") else True)
        )
        if is_similar and is_spec_pending:
            ambiguous.append(d)
        elif d.state in ("auto", "clarify") or (d.state == "review" and not is_similar):
            # 已确定规格（auto/clarify）或非相似组的普通 review：重量已知/可忽略
            definite.append(d)
        else:
            other_review.append(d)

    # 托盘上有无法归类的商品 → 重量不可靠，放弃自动推断
    if other_review:
        logger.info(
            "[DIAG] 重量终裁跳过: 存在 %d 个无法归类的 review 项，重量不可靠",
            len(other_review),
        )
        return
    if not ambiguous:
        return

    # 已确认实例的基准重量
    base_weight = 0.0
    for d in definite:
        sku = d.candidates[0].sku_id if d.candidates else None
        prod = products_by_sku.get(sku) if sku else None
        if prod:
            base_weight += prod.visual.weight_g * max(1, d.quantity)
    residual = tray_weight_g - tare - base_weight

    # 为每个歧义实例列出组内候选规格（去重）及对应单件重量
    cand_grams: list[list[int]] = []
    cand_weights: list[list[float]] = []
    for d in ambiguous:
        group = _group_of_sku(d.candidates[0].sku_id, products_by_sku)
        grams = []
        weights = []
        seen = set()
        for sku in group:
            p = products_by_sku.get(sku)
            if not p:
                continue
            g = _extract_grams(p.spec)
            if g is not None and g not in seen:
                seen.add(g)
                grams.append(g)
                weights.append(p.visual.weight_g)
        if not grams:
            # 该实例组里没有任何可解析规格，无法用重量推断
            logger.info("[DIAG] 重量终裁跳过: %s 组内无可用规格", d.box_id)
            return
        cand_grams.append(grams)
        cand_weights.append(weights)

    # 枚举所有组合，找「唯一」一个落在容差内的规格多重集。
    # 注意：重量只能确定「各规格的数量组合」（多重集），无法区分具体哪个实例是
    # 哪个规格（两袋外观一致，秤称不出谁左谁右）。所以按「排序后的规格元组」
    # 去重判定唯一性——(70,40) 与 (40,70) 视为同一多重集。
    winning_multisets: set[tuple[int, ...]] = set()
    best_err = None

    def _enumerate(idx: int, chosen: list[int], current: float):
        nonlocal best_err
        if idx == len(cand_grams):
            err = abs(current - residual)
            if err <= tolerance:
                winning_multisets.add(tuple(sorted(chosen)))
                if best_err is None or err < best_err:
                    best_err = err
            return
        for j, g in enumerate(cand_grams[idx]):
            chosen.append(g)
            _enumerate(idx + 1, chosen, current + cand_weights[idx][j])
            chosen.pop()

    _enumerate(0, [], 0.0)

    if len(winning_multisets) != 1:
        # 没有组合命中，或存在多个等价多重集 → 无法唯一确定，保持人工复核
        logger.info(
            "[DIAG] 重量终裁未决: residual=%.0fg 命中规格多重集=%d（需唯一），保持人工复核",
            residual, len(winning_multisets),
        )
        return

    chosen = sorted(next(iter(winning_multisets)))
    # 应用：把每个歧义实例切换到命中的规格（保留视觉判定的风味）。
    # 多重集按升序套到实例列表上即可——账单只关心各规格数量，不关心谁左谁右。
    for d, gram in zip(ambiguous, chosen):
        old_state = d.state
        target = _pick_sku_in_group(
            gram,
            _group_of_sku(d.candidates[0].sku_id, products_by_sku),
            products_by_sku,
            preferred_name=d.candidates[0].name if d.candidates else None,
        )
        if not target:
            continue
        reason = f"重量终裁推断{gram}g（实称残差{residual:.0f}g 唯一匹配{gram}g装）"
        _reassign_detection_sku(d, target, products_by_sku, reason)
        logger.info(
            "[DIAG] 重量终裁成功: %s %s→auto %s 推断%dg (residual=%.0f)g",
            d.box_id, old_state, target, gram, residual,
        )


def build_detections(
    raw_items: list,
    products_by_sku: dict[str, Product],
    boxes: list[tuple[float, float, float, float]],
    settings: Settings,
    lengths: list[float | None] | None = None,
    tray_weight_g: float | None = None,
) -> list[Detection]:
    """把模型输出转成检测结果。

    这一步不再做任何模糊检索：模型已经从清单里选定了 sku_id，
    这里只做校验、校准和状态判定。清单外的 sku_id 一律丢弃。
    """
    detections: list[Detection] = []

    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue

        quantity = max(1, int(raw.get("quantity", 1) or 1))
        x, y, w, h = boxes[idx] if idx < len(boxes) else (10.0, 30.0, 18.0, 20.0)
        # 包装上读到的文字。复核界面要展示它，店员据此一眼判断该选哪个规格，
        # 所以要限制长度，避免模型啰嗦时把界面撑坏。
        evidence = str(raw.get("ocr_text", "") or "").strip()[:60] or None

        raw_cands = raw.get("candidates") or []
        if not isinstance(raw_cands, list):
            raw_cands = []

        ranked: list[tuple[float, Product]] = []
        for c in raw_cands:
            if not isinstance(c, dict):
                continue
            sku = str(c.get("sku_id", "")).strip()
            product = products_by_sku.get(sku)
            if product is None:
                continue  # 清单外的 sku_id 不采信
            try:
                conf = min(max(float(c.get("confidence", 0.0)), 0.0), 1.0)
            except (TypeError, ValueError):
                conf = 0.0
            ranked.append((conf, product))
        ranked.sort(key=lambda t: -t[0])

        # —— 同款不同规格硬纠偏（演示）——
        # 视觉模型在「乐事 70g / 40g」这类外观几乎一致的商品上几乎无法区分，
        # 常常只输出 70g 一个候选，此时「候选内重排（需 ≥2 候选）」救不回来。
        # 这里做成能纠正单一候选的硬纠偏：只要 top 候选属于某个相似组，就用两类
        # 更可靠的信号重选款：
        #   1) OCR 净含量：包装印的「40g/70g」是印刷体数字，比看外观可靠；
        #   2) 尺子实测长度：YOLO 框精确像素 ÷ px_per_cm 得到商品外尺寸长边，
        #      与组内各款的 pkg_length_cm 比对，谁贴合用谁。
        # 两个信号都要求「切到组内另一款」才生效，否则信任视觉，避免噪声误翻转。
        meas = lengths[idx] if (lengths and idx < len(lengths)) else None
        ocr_text = str(raw.get("ocr_text", "") or "")
        if ranked and ((meas and meas > 0) or ocr_text):
            orig_sku = ranked[0][1].sku_id
            final_sku, reason = _resolve_similar_group(
                orig_sku, meas, ocr_text, products_by_sku
            )
            if final_sku != orig_sku:
                final_product = products_by_sku[final_sku]
                top_conf = ranked[0][0]
                # 保留原视觉置信度（模型确实看到了乐事薯片，只是分不清规格），
                # 把头名换成尺子/OCR 选中的那款，并去掉可能重复的后续候选。
                ranked = [(top_conf, final_product)] + [
                    (c, p) for c, p in ranked[1:] if p.sku_id != final_sku
                ]
                logger.info(
                    "同款规格纠偏: 视觉候选=%s, 实测=%.1fcm, OCR=%r => 切换为 %s (%s)",
                    orig_sku, meas or 0.0, ocr_text, final_sku, reason,
                )
                if reason:
                    evidence = f"{reason}->{final_product.name}"[:60]

        if not ranked:
            # 模型看到了东西却认不出来。如实记为未识别转人工复核，
            # 既不能随便塞一个商品进账单，也不能让它在账单里凭空消失。
            detections.append(
                Detection(
                    box_id=f"V{idx + 1}",
                    x=x, y=y, w=w, h=h,
                    candidates=[],
                    quantity=quantity,
                    state="review",
                    message="识别到商品但无法确定品类，已转入人工复核",
                    evidence=evidence,
                )
            )
            continue

        top_conf, top_product = ranked[0]
        runner_conf = ranked[1][0] if len(ranked) > 1 else 0.0
        gap = top_conf - runner_conf
        score = calibrate_confidence(top_conf, gap)

        candidates = [Candidate(sku_id=top_product.sku_id, name=top_product.name, score=score)]
        for conf, product in ranked[1:3]:
            if any(c.sku_id == product.sku_id for c in candidates):
                continue
            candidates.append(
                Candidate(
                    sku_id=product.sku_id,
                    name=product.name,
                    score=round(calibrate_confidence(conf, gap), 3),
                )
            )

        # 只有分差足够小的候选才算「对手」：追问是为了区分它们，
        # 分差很大的候选没有追问价值，不该拉低自动入账的门槛。
        has_competitors = any(conf >= top_conf - COMPETITOR_GAP for conf, _ in ranked[1:])

        # 同名不同规格兄弟款兜底：当商品存在「同名但不同规格」的兄弟款、
        # 且 OCR 净含量和尺子两条信号都不可用时，视觉模型的规格判定不可信
        # （VLM 分不清 40g/70g），降为 review 避免自动入账错误规格。
        # 店员在复核界面能看到两个候选手动选择。
        #
        # 判定依据是「同名不同规格」而非 SIMILAR_GROUPS 成员：易混包装组
        # （可口 / 百事、元气森林白桃 / 葡萄）属于不同商品，标签本身足以区分，
        # 视觉分差是有效证据；对它们强制复核只会徒增人工负担，
        # 并让「压倒性优势」这种本该自动入账的场景无谓报警。
        #
        # 但若 OCR 已确认 top 自身的规格（如 ocr 含"70克"且 top 就是 70g），
        # 则信任 OCR 判定、不强制 review——此时规格已确定，只是尺子不可信而已。
        force_review = False
        if top_product and _spec_siblings(top_product, products_by_sku):
            ocr_g = _extract_grams(ocr_text) if ocr_text else None
            top_visual_g = _extract_grams(top_product.spec)
            top_len = getattr(top_product.visual, "pkg_length_cm", 0.0) or 0.0
            meas_ok = (meas or 0) > 0 and (meas or 0) >= top_len * 0.6 if top_len > 0 else False
            ocr_confirms_top = (ocr_g is not None and ocr_g == top_visual_g)
            ocr_suggests_switch = (ocr_g is not None and ocr_g != top_visual_g)
            if not ocr_confirms_top and not ocr_suggests_switch and not meas_ok:
                force_review = True
                logger.info(
                    "[DIAG] 同名兄弟款强制review: sku=%s 无OCR净含量且尺子不可信(meas=%s)",
                    top_product.sku_id, meas,
                )
                evidence = f"疑似{top_product.name}（规格需人工确认，OCR未读到净含量）"
            elif ocr_confirms_top:
                logger.info(
                    "[DIAG] 相似组OCR确认: sku=%s OCR确认规格%d%s 与视觉一致，跳过review",
                    top_product.sku_id, ocr_g, "克" if "克" in (ocr_text or "") else "g",
                )

        state = decide_state(score, settings, has_competitors=has_competitors)
        if force_review and state == "auto":
            state = "review"

        det = Detection(
            box_id=f"V{idx + 1}",
            x=x, y=y, w=w, h=h,
            candidates=candidates,
            quantity=quantity,
            state=state,
            message=build_message(state, candidates),
            evidence=evidence,
        )
        det._meas = meas  # 尺寸排序分配用（不序列化到 API 响应）
        detections.append(det)

    # ── Phantom 守卫：面积过小的检测框通常是 YOLO 对软包装的局部/杂框误检
    #    （如大袋角落被单独检出、错判成完全不同的商品），强制降为 review 防止幽灵入账。
    #    阈值单位：归一化坐标 w% × h%（即图像面积的万分之几）。
    #    实测：真实商品袋 > 400，V4 幽灵沙琪玛 = 138，V3 杂框 = 149。
    _MIN_BOX_AREA_PCT2 = 150
    for det in detections:
        area = det.w * det.h
        if area < _MIN_BOX_AREA_PCT2 and det.state == "auto":
            old_state = det.state
            det.state = "review"
            det.evidence = f"疑似误检（框面积{area:.0f}%²过小，需人工确认）"
            logger.info(
                "[DIAG] Phantom guard: %s area=%.0f < %d => %s→review",
                det.box_id, area, _MIN_BOX_AREA_PCT2, old_state,
            )

    # ── 相似组尺寸排序分配：当同组商品有多件且 OCR 都没读到净含量时，
    #    用尺子实测长度的相对排序来区分不同规格。
    #    YOLO 框偏小导致绝对测量值不可信（~50% 真实），但同一帧内相对大小
    #    关系是正确的（大袋实测 > 小袋实测）。结合组内 SKU 的标定长度排序，
    #    按秩匹配即可推断每件是哪个规格——前提是件数 ≤ 组内规格数。
    _resolve_by_size_ranking(detections, products_by_sku)

    # ── 相似组重量终裁：当 OCR/尺子/尺寸排序都分不清规格时，
    #    用整盘总重做组合约束——哪种规格分配的总重最接近实测总重就选哪种。
    #    优先级：OCR 最高优先，重量仅作最后终裁，绝不覆盖已 OCR 确认的规格。
    _resolve_by_weight(detections, tray_weight_g, products_by_sku, settings)

    return detections


async def call_vision_api(
    image_base64: str,
    settings: Settings,
    cropped: bool = False,
) -> list[dict]:
    """调用一次视觉模型，返回原始 items 列表（未做校验与校准）。

    抽出来是为了让单帧、多帧与级联三种模式共用同一条调用链路：
    它们的差别只在「喂什么图、怎么聚合」，调用本身不该有第二份实现。
    """
    products = product_repo.list_products()

    payload = {
        "model": settings.VISION_MODEL,
        "temperature": 0,
        # 关闭思考：闭卷选择不需要多步推理，开了只会让响应慢一个数量级
        "enable_thinking": settings.VISION_ENABLE_THINKING,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                    {
                        "type": "text",
                        "text": build_vision_prompt(build_catalog(products), cropped=cropped),
                    },
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.VISION_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=settings.VISION_TIMEOUT) as client:
        resp = await client.post(
            f"{settings.VISION_BASE_URL.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    data = _extract_json(content)
    raw_items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        raise ValueError("模型返回格式不正确")
    return raw_items


async def probe_ruler_scale(image_base64: str, settings: Settings) -> float | None:
    """探测画面中尺子的比例尺（px_per_cm）。

    演示用：每帧让视觉大模型读一次尺子刻度，返回「每厘米对应多少像素」。
    返回 None 表示画面里没尺子或读取失败，调用方应跳过尺寸纠偏、不强行参与判断。

    只在 RULER_ENABLED 时调用，且复用与商品识别相同的传输通道（同一模型、同一密钥）。
    """
    if not settings.RULER_ENABLED:
        return None
    payload = {
        "model": settings.VISION_MODEL,
        "temperature": 0,
        "enable_thinking": settings.VISION_ENABLE_THINKING,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                    {"type": "text", "text": RULER_PROBE_PROMPT},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.VISION_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.VISION_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.VISION_BASE_URL.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        if isinstance(data, dict) and data.get("has_ruler"):
            # 优先取「每厘米对应像素数」；若模型直接回了尺子总像素长，用已知真实尺长换算
            px = data.get("px_per_cm")
            if isinstance(px, (int, float)) and px > 0:
                return float(px)
            total_px = data.get("ruler_px") or data.get("length_px") or data.get("ruler_length_px")
            if isinstance(total_px, (int, float)) and total_px > 0 and settings.RULER_LENGTH_CM > 0:
                return float(total_px) / settings.RULER_LENGTH_CM
    except Exception as exc:  # 尺子探测失败绝不该拖垮结账链路，记日志后退回「无比例尺」
        logger.warning("尺子比例尺探测失败，跳过尺寸纠偏: %s", exc)
    return None


# ── 纯 OCR 专用提示词（用于区分同款不同规格时补充净含量数字）──
OCR_ONLY_PROMPT = """\
请仔细读取这张商品包装图片上的所有文字，特别注意：
1. 包装底部或边角印的「净含量」数字（如 40g、70g、97g、330ml 等）
2. 规格信息（如「袋装」「盒装」等）

只返回你读到的原始文字，不要做任何分类或判断。
如果完全看不到文字或这不是商品包装，返回空字符串。

格式（严格 JSON）：
{"text": "你读到的所有文字内容"}\
"""


async def call_ocr_api(image_b64: str, settings: Settings) -> str:
    """对裁剪图做纯 OCR 调用，专门提取净含量等规格数字。

    与 call_vision_api 的区别：不做分类/选品，只读文字。
    用于相似组商品场景——分类调用的 ocr_text 常漏掉边角的净含量数字，
    用这个专用 OCR 补充一次机会。
    返回空字符串表示没读到有用文字。
    """
    payload = {
        "model": settings.VISION_MODEL,
        "temperature": 0,
        "enable_thinking": False,  # OCR 不需要思考，关掉加速
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": OCR_ONLY_PROMPT},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.VISION_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.VISION_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.VISION_BASE_URL.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        text = str(data.get("text", "") or "").strip() if isinstance(data, dict) else ""
        if text:
            logger.info("[DIAG] 纯OCR结果: %r", text[:120])
        return text
    except Exception as exc:
        logger.warning("纯OCR调用失败: %s", exc)
        return ""


async def recognize_vision(
    image_base64: str,
    settings: Settings,
    tray_weight_g: float | None = None,
) -> list[Detection]:
    products = product_repo.list_products()
    products_by_sku = {p.sku_id: p for p in products}
    raw_items = await call_vision_api(image_base64, settings)
    detections = build_detections(
        raw_items, products_by_sku, _placeholder_boxes(len(raw_items)),
        settings, tray_weight_g=tray_weight_g,
    )
    _resolve_by_elimination(detections, products_by_sku)
    return detections


def _consistency_factor(votes: int, frames: int) -> float:
    """按「多少帧投了它」折算置信度。

    单帧看错往往只是那一帧反光或角度不好，多帧都指向同一个答案才可信。
    三帧各说各话时，头名也只有三分之一的支持率，必须明显下调分数，
    让仲裁层把它推进追问或人工复核，而不是当共识接受。
    """
    ratio = votes / max(frames, 1)
    if ratio >= 0.99:
        return 1.0
    if ratio >= 0.66:
        return 0.92
    if ratio >= 0.5:
        return 0.80
    return 0.60


def majority_quantity(values: list[int]) -> int:
    """取众数，并列时取较大值——宁可多算一件让称重去报警，也别少算一件让顾客白拿。"""
    if not values:
        return 1
    counts = Counter(values)
    top = max(counts.values())
    return max(v for v, c in counts.items() if c == top)


def pick_ocr_text(texts: list[str]) -> str:
    """多帧读到的包装文字取出现次数最多的，并列时取最长的一条（信息更全）。"""
    if not texts:
        return ""
    counts = Counter(texts)
    top = max(counts.values())
    return max((t for t, c in counts.items() if c == top), key=len)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def vote_threshold(frames: int) -> int:
    """一件商品要单独成条，至少需要几帧支持：过半数。

    这条规则是为了防止重复计费。三帧分别认成 40g / 70g / 40g 时，
    若两个 SKU 各自成条，顾客会替同一包薯片付两次钱。票数不足的只能
    作为对手候选出现，由分差去触发追问。
    """
    return 2 if frames >= 3 else 1


def tally_votes(
    frame_entries: list[list[dict]],
    products_by_sku: dict[str, Product],
) -> tuple[list[tuple[str, dict]], int]:
    """统计每个 SKU 在多帧里得到的票数与分数。

    抽出来是为了让「多帧直接投票」与「级联模式投票」共用同一套计票规则。
    两边规则一旦漂移，同一件商品会在两条路径上算出不同的置信度，
    这种不一致在演示中表现为「换个开关结果就变」，极难排查。

    frame_entries 每帧一个列表，每项形如
      {"sku_id":..., "confidence":..., "quantity":..., "ocr_text":..., "box": ...}
    其中 box 等额外字段会被原样保留在 extras 里，供级联模式取回真实坐标。
    """
    frames = len(frame_entries)
    stats: dict[str, dict] = {}

    for entries in frame_entries:
        seen_in_frame: set[str] = set()
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            sku = str(raw.get("sku_id", "")).strip()
            if sku not in products_by_sku:
                continue  # 清单外的 sku_id 连票都不该投

            try:
                conf = min(max(float(raw.get("confidence", 0.0)), 0.0), 1.0)
            except (TypeError, ValueError):
                conf = 0.0
            qty = max(1, int(raw.get("quantity", 1) or 1))
            ocr = str(raw.get("ocr_text", "") or "").strip()

            st = stats.setdefault(
                sku, {"scores": [], "votes": 0, "qtys": [], "ocrs": [], "extras": []}
            )
            st["scores"].append(conf)
            st["qtys"].append(qty)
            if ocr:
                st["ocrs"].append(ocr)
            st["extras"].append(raw)
            # 同一帧里同一 SKU 只算一票，避免模型重复输出时权重虚高
            if sku not in seen_in_frame:
                st["votes"] += 1
                seen_in_frame.add(sku)

    ordered = sorted(stats.items(), key=lambda kv: (-kv[1]["votes"], -_mean(kv[1]["scores"])))
    return ordered, frames


def build_voted_candidates(
    ordered: list[tuple[str, dict]],
    frames: int,
    top_sku: str,
) -> list[dict]:
    """把排序后的票型转成 candidates 列表，并把一致性折扣乘进置信度。

    折扣在这里就应用，因为下游的 calibrate_confidence 只看分差、拿不到票数，
    两层折扣叠加才能既压低「三帧各说各话」，又保留难分伯仲时的区分度。
    """
    top_stats = next(st for sku, st in ordered if sku == top_sku)
    top_factor = _consistency_factor(top_stats["votes"], frames)
    candidates = [
        {
            "sku_id": top_sku,
            "confidence": round(min(_mean(top_stats["scores"]) * top_factor, 1.0), 3),
        }
    ]
    for other_sku, other_st in ordered:
        if other_sku == top_sku or other_st["votes"] >= top_stats["votes"]:
            continue
        factor = _consistency_factor(other_st["votes"], frames)
        candidates.append(
            {
                "sku_id": other_sku,
                "confidence": round(min(_mean(other_st["scores"]) * factor, 1.0), 3),
            }
        )
    return candidates[:3]


def aggregate_votes(
    frame_items: list[list[dict]],
    products_by_sku: dict[str, Product],
) -> list[dict]:
    """把多帧的模型原始输出投票聚合成一份 items。"""
    entries_per_frame: list[list[dict]] = []
    for items in frame_items:
        entries: list[dict] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            cands = raw.get("candidates")
            if not isinstance(cands, list) or not cands:
                continue
            qty = max(1, int(raw.get("quantity", 1) or 1))
            ocr = str(raw.get("ocr_text", "") or "").strip()
            for c in cands:
                if not isinstance(c, dict):
                    continue
                entries.append(
                    {
                        "sku_id": str(c.get("sku_id", "")).strip(),
                        "confidence": c.get("confidence", 0.0),
                        "quantity": qty,
                        "ocr_text": ocr,
                    }
                )
        entries_per_frame.append(entries)

    ordered, frames = tally_votes(entries_per_frame, products_by_sku)
    if not ordered:
        return []

    threshold = vote_threshold(frames)
    independents = [(sku, st) for sku, st in ordered if st["votes"] >= threshold]

    # 没有任何 SKU 得到过半支持：三帧各说各话。只留头名一条，
    # 让所有候选并驾齐驱，由分差把它压到人工复核，绝不替顾客拍板。
    if not independents:
        independents = ordered[:1]

    items: list[dict] = []
    for sku, st in independents:
        items.append(
            {
                "candidates": build_voted_candidates(ordered, frames, sku),
                "quantity": majority_quantity(st["qtys"]),
                "ocr_text": pick_ocr_text(st["ocrs"]),
            }
        )
    return items


async def recognize_vision_frames(
    images: list[str],
    settings: Settings,
    tray_weight_g: float | None = None,
) -> list[Detection]:
    """连拍多帧投票识别：并行调用每帧，再按一致性聚合。

    单帧判断容易被反光、遮挡、角度干扰，多帧取共识是成本最低的稳定性提升。
    任一帧失败不拖垮整体——只要还有帧成功就继续，全失败才如实报错。
    """
    products = product_repo.list_products()
    products_by_sku = {p.sku_id: p for p in products}

    results = await asyncio.gather(
        *[call_vision_api(img, settings) for img in images],
        return_exceptions=True,
    )

    frame_items: list[list[dict]] = []
    failures: list[str] = []
    for r in results:
        if isinstance(r, BaseException):
            failures.append(f"{type(r).__name__}: {r}"[:120])
        else:
            frame_items.append(r)

    if not frame_items:
        raise ValueError(f"全部 {len(images)} 帧识别失败：{'; '.join(failures)}")

    items = aggregate_votes(frame_items, products_by_sku)
    # 部分帧失败时一致性分母变小，票数门槛会自动放宽，不会因此误判
    detections = build_detections(
        items, products_by_sku, _placeholder_boxes(len(items)),
        settings, tray_weight_g=tray_weight_g,
    )
    _resolve_by_elimination(detections, products_by_sku)
    return detections


# 检测器按权重路径缓存；加载失败的权重记进 _broken_detectors，
# 避免每个请求都重试一次注定失败的加载，白白拖长响应时间。
_detector_cache: dict[str, object] = {}
_broken_detectors: set[str] = set()


def get_detector(settings: Settings):
    """按配置取检测器，不可用就返回 None。

    级联是增强项而不是依赖项：无论没开启、权重缺失还是加载失败，
    都只是退回整图识别，绝不让结账流程走不下去。
    """
    if settings.DETECTOR != "yolo":
        return None

    key = settings.YOLO_WEIGHTS
    if key in _broken_detectors:
        return None
    if key in _detector_cache:
        return _detector_cache[key]

    try:
        from app.services.detectors.yolo import YoloCocoDetector

        detector = YoloCocoDetector(
            weights=key,
            conf=settings.YOLO_CONF,
            max_boxes=settings.YOLO_MAX_BOXES,
            nms_iou=settings.YOLO_NMS_IOU,
        )
        # 权重在这里就加载掉。路径写错这类问题要在系统启动时暴露，
        # 而不是等顾客按下拍照键才发现。
        detector.warmup()
    except Exception as exc:
        logger.warning("YOLO 检测器不可用，已回退整图识别: %s", exc)
        _broken_detectors.add(key)
        return None

    _detector_cache[key] = detector
    return detector


async def _recognize_pipeline(
    frames: list[str],
    settings: Settings,
    tray_weight_g: float | None = None,
) -> tuple[list[Detection], str]:
    """真实识别流水线：能级联就级联，级联拿不到可用结果就退回整图。"""
    detector = get_detector(settings)
    if detector is not None:
        try:
            from app.services.cascade import recognize_cascade

            result = await recognize_cascade(frames, settings, detector, tray_weight_g=tray_weight_g)
            if result is not None:
                return result, f"vision-cascade:{detector.name}"
        except Exception as exc:  # 级联失败不该连累整条结账链路
            logger.warning("级联识别失败，回退整图识别: %s", exc)

    if len(frames) > 1:
        return await recognize_vision_frames(frames, settings, tray_weight_g=tray_weight_g), "vision-vote"
    return await recognize_vision(frames[0], settings, tray_weight_g=tray_weight_g), "vision"


async def recognize(
    scene_id: str | None,
    image_base64: str | None,
    use_vision_model: bool,
    settings: Settings,
    images: list[str] | None = None,
    tray_weight_g: float | None = None,
) -> tuple[list[Detection], str, int]:
    """统一的识别入口，返回 (检测列表, 模式标识, 耗时毫秒)。

    模式标识的语义必须严格区分，前端据此决定信任程度：
      vision                 —— 真实视觉模型识别成功（框是按数量均分的占位框）
      vision-vote            —— 连拍多帧投票，结果取多帧共识
      vision-cascade:yolo-*  —— 检测器出真实框 + 大模型判类别
      vision-failed          —— 真实视觉模型调用失败，如实返回空结果
      demo:S1                —— 示例场景的预设数据，只用于演示界面流程

    关键约束：拍照是真实结账路径，模型看不了就必须如实说看不了，
    绝不能退回示例场景数据顶替——那会让顾客为一堆他根本没拿的商品买单。
    """
    started = time.perf_counter()

    # images 优先：连拍多帧时前端传数组，单张图仍走 image_base64
    frames = [f for f in (images or []) if f]
    if not frames and image_base64:
        frames = [image_base64]

    if use_vision_model and settings.VISION_API_KEY and frames:
        try:
            detections, mode = await _recognize_pipeline(frames, settings, tray_weight_g=tray_weight_g)
            elapsed = int((time.perf_counter() - started) * 1000)
            _set_vision_error(None)
            return detections, mode, elapsed
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            _set_vision_error(f"{type(exc).__name__}: {exc}"[:300])
            elapsed = int((time.perf_counter() - started) * 1000)
            return [], "vision-failed", elapsed

    # 示例场景：预设的演示数据，如实标注为 demo，不代表真实识别能力
    detections, scene_used = recognize_mock(scene_id, settings)
    # 模拟推理耗时，让前端的进度表现更接近真实体感
    await asyncio.sleep(0.6)
    elapsed = int((time.perf_counter() - started) * 1000)
    return detections, f"demo:{scene_used}", elapsed
