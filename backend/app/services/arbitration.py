"""置信度仲裁与重量校验。

这是结账准确率的守门人：视觉分不是唯一依据，重量差作为独立信号交叉验证。
三态输出：auto（自动入账）/ clarify（主动追问）/ review（转人工复核）。
"""

from __future__ import annotations

from app.core.config import Settings
from app.models.schemas import Candidate, Detection
from app.repositories import product_repo
from app.services.pricing import Line


def decide_state(score: float, settings: Settings, has_competitors: bool = False) -> str:
    """判定检测项去向。

    has_competitors 为 False 说明商品库里没有能与之混淆的对象，
    此时阈值可以放宽——识别结果虽不完美，但也没有更好的选择，追问没有意义。
    """
    if score >= settings.AUTO_ACCEPT_SCORE:
        return "auto"
    if not has_competitors and score >= settings.SINGLE_CANDIDATE_SCORE:
        return "auto"
    if score >= settings.CLARIFY_SCORE:
        return "clarify"
    return "review"


def build_message(state: str, candidates: list[Candidate], occluded: bool = False) -> str:
    if state == "review":
        if occluded:
            return "该商品被其他商品遮挡，无法确认，已转入人工复核"
        return "识别置信度过低，已转入人工复核"
    if state == "clarify" and len(candidates) > 1:
        names = " / ".join(c.name for c in candidates[:2])
        return f"与相似包装难以区分（{names}），请确认"
    if state == "auto":
        return ""
    return "识别结果不够确定，请确认"


def weight_check(
    detections: list[Detection],
    tray_weight_g: float | None,
    settings: Settings,
    resolved: dict[str, str] | None = None,
) -> dict | None:
    """用称重读数校验账单商品是否匹配，发现漏检或多放。

    只统计「已入账」的商品，判定口径必须与 to_bill_lines 完全一致：
    转人工的项目在确认前不计入，差额正好暴露漏检；一旦人工补入就要计入，
    否则顾客明明已经确认，却还一直提示疑似漏检。
    """
    if tray_weight_g is None:
        return None
    resolved = resolved or {}

    expected = 0.0
    for d in detections:
        if d.state == "review" and d.box_id not in resolved:
            continue
        chosen_sku = resolved.get(d.box_id) or (d.candidates[0].sku_id if d.candidates else None)
        product = product_repo.get_product(chosen_sku) if chosen_sku else None
        if product:
            expected += product.visual.weight_g * max(1, d.quantity)

    diff = round(tray_weight_g - expected, 1)
    tolerance = settings.WEIGHT_TOLERANCE_G
    status = "ok" if abs(diff) <= tolerance else ("suspicious_high" if diff > 0 else "suspicious_low")

    message = {
        "ok": f"称重校验通过（实称 {tray_weight_g:.0f}g / 应重 {expected:.0f}g）",
        "suspicious_high": f"实称比账单商品重 {diff:.0f}g，可能有商品未被识别，请摊开重拍",
        "suspicious_low": f"实称比账单商品轻 {abs(diff):.0f}g，请检查是否有商品未放置",
    }[status]

    return {
        "status": status,
        "tray_weight_g": tray_weight_g,
        "expected_weight_g": round(expected, 1),
        "diff_g": diff,
        "tolerance_g": tolerance,
        "message": message,
    }


def to_bill_lines(
    detections: list[Detection],
    resolved: dict[str, str] | None = None,
) -> list[Line]:
    """把检测结果转成账单行。resolved 记录用户在追问中确认的 SKU。"""
    resolved = resolved or {}
    lines: list[Line] = []

    for d in detections:
        # 候选可以为空：人工录入的条目没有模型候选，靠 resolved 指定商品
        chosen_sku = resolved.get(d.box_id) or (d.candidates[0].sku_id if d.candidates else None)
        if not chosen_sku:
            continue
        product = product_repo.get_product(chosen_sku)
        if product is None:
            continue

        if d.state == "review":
            # 低置信项只有被人工确认后才允许入账
            if d.box_id not in resolved:
                continue
            source = "manual"
        else:
            source = "clarify" if d.box_id in resolved else "auto"

        matched = next((c for c in d.candidates if c.sku_id == chosen_sku), None)
        # 人工录入没有模型打分，置信度按 100% 计——是人确认过的
        confidence = matched.score if matched else (d.candidates[0].score if d.candidates else 1.0)

        lines.append(
            Line(product=product, quantity=max(1, d.quantity), source=source, confidence=confidence)
        )

    return lines
