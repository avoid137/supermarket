"""级联识别：检测器出真实框，视觉大模型判类别。

为什么这么分工：
    视觉大模型能认出商品，却给不出坐标——它返回的 items 只有清单和数量，
    前端那些框是按数量在盘面上均分出来的占位框，与商品实际位置无关。
    检测器恰好相反：它圈得准，但 COCO 那 80 个类里没有零售包装，
    认不出是乐事还是好丽友。两者一拼，就能拿到真实坐标 + 准确品类。

为什么不担心检测器认错东西：
    检测器只负责「这里有个独立物体」，判断交给大模型。即便它把薯片袋
    当成 book 框出来，框的位置仍然是对的，裁出来的图大模型照样认得。

回退是这套设计的底线：
    检测器一个框都没提出来，或裁出来的图全被判为清单外商品时，
    本模块返回 None，由调用方退回整图识别。级联是增强，不是唯一通路——
    检测器不工作时，识别能力必须至少与不开级联时持平。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from PIL import Image

logger = logging.getLogger(__name__)

from app.core.config import Settings
from app.data.products import SIMILAR_GROUPS
from app.models.schemas import Detection
from app.repositories import product_repo
from app.services import vision
from app.services.detectors.base import BoxProposal, Detector  # noqa: F401

# 裁剪时向外扩展的比例。框贴着商品边缘时，包装上的规格文字常在边缘被切掉，
# 留一点边距能显著提高 OCR 读到净含量的概率——这恰好是区分同款不同规格的关键。
# 对软包装（薯片袋等）YOLO 框往往只覆盖中心图案区域，需要更大的边距才能
# 包含印在底部/角落的「70g / 40g」净含量文字。
CROP_PADDING = 0.25

# 纯 OCR 补充调用专用边距序列（按序重试，直到某一次读到规格单位）。
# 分类框对软包装只覆盖中心图案（约真实尺寸 50%），净含量数字印在包装
# 底部/角落、常落在框外。几何上要够到袋底文字至少需要 padding≈0.5，
# 但过大又会稀释分辨率导致 VLM 读不到。所以从小到大多试几次，用第一个
# 命中规格的结果，兼顾「够到边角」与「保留分辨率」。
OCR_SUPPLEMENT_PADDINGS = (0.35, 0.55, 0.80)

# 裁剪结果最小边长（像素）。比这更小的图送到大模型也只剩噪声，直接丢弃。
MIN_CROP_PX = 32


def crop_bottom_focus(image_bytes: bytes, box: BoxProposal) -> str:
    """裁剪商品包装底部区域——净含量/规格数字常印在袋底边角。

    YOLO 框只覆盖中心图案区域（约真实尺寸 50%），而「70g」「40克」等
    净含量文字印在包装底部或右下角，落在框外。均匀放大裁剪（padding）
    会把整袋+背景一起送进去、稀释分辨率。本函数改为只切「框下方到估
    计袋底」这一条带，以更高的有效分辨率呈现角落里的规格文字。
    返回空字符串表示裁剪区域太小（不足 MIN_CROP_PX），调用方应跳过。
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size

    # 框底部像素坐标
    box_bot_px = ((box.y + box.h) / 100) * height

    # 估计袋底：软包装袋的真实高度约为 YOLO 框高的 1.8~2.2 倍，
    # 框中心偏上（图案区），所以袋底大约在框底下方的 0.6~1.0 倍框高处。
    # 取 0.8 倍作为保守估计，保证不切掉袋底文字。
    box_h_px = (box.h / 100) * height
    bag_bot_px = min(float(height), box_bot_px + box_h_px * 0.8)

    # 裁剪宽度：框宽的 1.3 倍（覆盖左右边角）
    cx_px = (box.x + box.w / 2) / 100 * width
    crop_w_px = int((box.w / 100) * width * 1.3)
    half_w = crop_w_px // 2

    x1 = max(0, int(cx_px - half_w))
    x2 = min(width, int(cx_px + half_w))
    # 从框底稍往上 3%（留点衔接），到估计袋底
    y1 = max(0, int(box_bot_px - height * 0.03))
    y2 = min(height, int(bag_bot_px))

    if y2 - y1 < MIN_CROP_PX or x2 - x1 < MIN_CROP_PX:
        return ""

    crop = img.crop((int(x1), int(y1), int(x2), int(y2)))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode()


def crop_box(image_bytes: bytes, box: BoxProposal, padding: float = CROP_PADDING) -> str:
    """按框裁剪出单个商品并返回 base64 JPEG。"""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size

    cx = (box.x + box.w / 2) / 100 * width
    cy = (box.y + box.h / 2) / 100 * height
    bw = box.w / 100 * width * (1 + padding * 2)
    bh = box.h / 100 * height * (1 + padding * 2)

    x1 = max(0.0, cx - bw / 2)
    y1 = max(0.0, cy - bh / 2)
    x2 = min(float(width), cx + bw / 2)
    y2 = min(float(height), cy + bh / 2)

    crop = img.crop((int(x1), int(y1), int(x2), int(y2)))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _mean_box(boxes: list[BoxProposal]) -> tuple[float, float, float, float]:
    """多帧里同一个商品的框取平均。

    连拍时手会抖、商品会轻微位移，逐帧坐标并不一致。取平均比取某一帧
    更能代表真实位置，也避免画面上框来回跳。
    """
    n = len(boxes)
    return (
        round(sum(b.x for b in boxes) / n, 2),
        round(sum(b.y for b in boxes) / n, 2),
        round(sum(b.w for b in boxes) / n, 2),
        round(sum(b.h for b in boxes) / n, 2),
    )


def _box_length_cm(
    box: BoxProposal, width: int, height: int, scale: float | None
) -> float | None:
    """用尺子比例尺把 YOLO 框换算成商品外尺寸长边（cm）。

    box 的坐标是归一化 0~100 的百分比，需要先乘回真实像素；
    长边取宽/高像素的较大者，再除以当帧 px_per_cm 得到厘米。
    scale 为 None（画面里没尺子 / 探测失败）时返回 None，调用方据此跳过尺寸纠偏。
    """
    if not scale or scale <= 0:
        return None
    px_w = box.w / 100.0 * width
    px_h = box.h / 100.0 * height
    return max(px_w, px_h) / scale


def _box_iou(a: BoxProposal, b: BoxProposal) -> float:
    """归一化框的交并比，用于跨帧把同一商品的多个框关联成一个实例。"""
    ax1, ay1, ax2, ay2 = a.x, a.y, a.x + a.w, a.y + a.h
    bx1, by1, bx2, by2 = b.x, b.y, b.x + b.w, b.y + b.h
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _cluster_instances(frame_entries: list[list[dict]]) -> list[dict]:
    """把跨帧、有真实坐标的识别条目按位置聚成「商品实例」。

    每个 YOLO 框是一个潜在实例；跨帧里位置重叠（IoU 够大）的框归到同一个簇，
    簇代表框取各帧平均。同帧里不重叠的多个框自然分属不同簇——这正是
    「一张照片里两包薯片」能各算一件的关键。
    """
    clusters: list[dict] = []
    for entries in frame_entries:
        for e in entries:
            box = e.get("box")
            if not isinstance(box, BoxProposal):
                continue
            best = None
            best_iou = 0.3  # 跨帧同一商品的位置重叠下限
            for c in clusters:
                iou = _box_iou(c["box"], box)
                if iou > best_iou:
                    best_iou = iou
                    best = c
            if best is None:
                clusters.append(
                    {
                        "box": box,
                        "box_sum": [box.x, box.y, box.w, box.h],
                        "box_n": 1,
                        "cands": [(str(e.get("sku_id", "")).strip(), float(e.get("confidence", 0.0)))],
                        "ocrs": [e["ocr_text"]] if e.get("ocr_text") else [],
                        "lengths": [e["measured_length_cm"]]
                        if e.get("measured_length_cm") is not None
                        else [],
                    }
                )
            else:
                b = best
                b["box_sum"] = [
                    b["box_sum"][0] + box.x,
                    b["box_sum"][1] + box.y,
                    b["box_sum"][2] + box.w,
                    b["box_sum"][3] + box.h,
                ]
                b["box_n"] += 1
                b["cands"].append((str(e.get("sku_id", "")).strip(), float(e.get("confidence", 0.0))))
                if e.get("ocr_text"):
                    b["ocrs"].append(e["ocr_text"])
                if e.get("measured_length_cm") is not None:
                    b["lengths"].append(e["measured_length_cm"])
                n = b["box_n"]
                b["box"] = BoxProposal(
                    x=b["box_sum"][0] / n,
                    y=b["box_sum"][1] / n,
                    w=b["box_sum"][2] / n,
                    h=b["box_sum"][3] / n,
                )
    return clusters


def _vote_instance(inst: dict, products_by_sku: dict):
    """对单个实例簇做投票，返回 (sku, candidates, ocr, 平均框, 实测长度)。

    实例内先按 SKU 聚合票数，取票数最高（并列取置信度和最高）为头名；
    再用尺子实测长度 / OCR 净含量做同款规格纠偏（只换规格、不串风味）；
    最后复用 build_voted_candidates 生成带一致性格折扣的候选列表。
    """
    from collections import defaultdict

    sku_scores: dict[str, list[float]] = defaultdict(list)
    for sku, conf in inst["cands"]:
        if sku in products_by_sku:
            sku_scores[sku].append(conf)
    if not sku_scores:
        return None, [], "", None, None

    local_ordered = [(sku, {"scores": sc, "votes": len(sc)}) for sku, sc in sku_scores.items()]
    frames = inst["box_n"]
    top_sku = max(local_ordered, key=lambda kv: (kv[1]["votes"], sum(kv[1]["scores"])))[0]
    cands = vision.build_voted_candidates(local_ordered, frames, top_sku)

    meas = sum(inst["lengths"]) / len(inst["lengths"]) if inst["lengths"] else None
    ocr = vision.pick_ocr_text(inst["ocrs"])
    logger.info(
        "[DIAG] _vote_instance: top_sku=%r meas=%r ocr=%r cands=%s",
        top_sku, meas, ocr[:60] if ocr else "", inst["cands"][:3],
    )
    final_sku, reason = vision._resolve_similar_group(top_sku, meas, ocr, products_by_sku)
    if final_sku != top_sku:
        top_conf = cands[0]["confidence"] if cands else 0.0
        cands = [{"sku_id": final_sku, "confidence": top_conf}] + [
            c for c in cands if c["sku_id"] != final_sku
        ]
        if reason:
            ocr = f"{reason}->{products_by_sku[final_sku].name}"[:60] or ocr

    n = inst["box_n"]
    # build_detections 期望 (x, y, w, h) 元组，这里把均值框转成元组传出
    box = (
        inst["box_sum"][0] / n,
        inst["box_sum"][1] / n,
        inst["box_sum"][2] / n,
        inst["box_sum"][3] / n,
    )
    return final_sku, cands, ocr, box, meas


async def recognize_cascade(
    images: list[str],
    settings: Settings,
    detector: Detector,
    tray_weight_g: float | None = None,
) -> list[Detection] | None:
    """级联识别。返回 None 表示级联没拿到可用结果，调用方应回退到整图识别。"""
    products = product_repo.list_products()
    products_by_sku = {p.sku_id: p for p in products}
    loop = asyncio.get_running_loop()

    async def process_frame(image_b64: str, scale: float | None) -> list[dict]:
        """单帧：检测 → 裁剪 → 逐个分类，摊平成计票条目。

        scale 为当帧尺子比例尺（px_per_cm，由视觉模型读尺子刻度得到）。
        非 None 时，用 YOLO 框的精确像素 ÷ scale 算出每个商品的外尺寸长边（cm），
        作为「尺子区分同款不同规格」的实测依据，随条目一起回传。
        """
        raw = base64.b64decode(image_b64)
        width, height = Image.open(io.BytesIO(raw)).size
        # ultralytics 是同步 API，直接调用会卡住整个事件循环，
        # 丢进线程池才能让它与网络请求真正并发。
        boxes = await loop.run_in_executor(None, detector.propose, raw)
        if not boxes:
            return []

        crops = [crop_box(raw, b) for b in boxes]
        results = await asyncio.gather(
            *[vision.call_vision_api(c, settings, cropped=True) for c in crops],
            return_exceptions=True,
        )

        entries: list[dict] = []
        for box, res in zip(boxes, results):
            if isinstance(res, BaseException):
                continue
            if not isinstance(res, list):
                continue
            for item in res:
                if not isinstance(item, dict):
                    continue
                cands = item.get("candidates")
                if not isinstance(cands, list) or not cands:
                    continue
                qty = max(1, int(item.get("quantity", 1) or 1))
                ocr = str(item.get("ocr_text", "") or "").strip()
                for c in cands:
                    if not isinstance(c, dict):
                        continue
                    entries.append(
                        {
                            "sku_id": str(c.get("sku_id", "")).strip(),
                            "confidence": c.get("confidence", 0.0),
                            "quantity": qty,
                            "ocr_text": ocr,
                            "box": box,
                            "measured_length_cm": _box_length_cm(box, width, height, scale),
                        }
                    )

        # ── 纯 OCR 补充：分类调用常漏掉包装边角的净含量数字（如 40g/70g），
        #    对没有读到规格单位的条目额外做一次专用 OCR 调用。
        #    仅当原始 ocr_text 不含「g」「ml」「L」等规格单位时才触发，避免重复调用。
        _has_spec_unit = lambda t: bool(re.search(r"\d+\s*(g|ml|L|kg|克)\b", t or "", re.I))
        ocr_supplement_indices = [i for i, e in enumerate(entries) if e.get("box") and not _has_spec_unit(e.get("ocr_text", ""))]
        if ocr_supplement_indices:
            for pad in OCR_SUPPLEMENT_PADDINGS:
                # 只补还没读到规格的条目，已命中的不再重复调用
                pending = [i for i in ocr_supplement_indices if not _has_spec_unit(entries[i].get("ocr_text", ""))]
                if not pending:
                    break
                supplement_crops = [crop_box(raw, entries[i]["box"], padding=pad) for i in pending]
                ocr_results = await asyncio.gather(
                    *[vision.call_ocr_api(c, settings) for c in supplement_crops],
                    return_exceptions=True,
                )
                for idx, ocr_result in zip(pending, ocr_results):
                    if isinstance(ocr_result, Exception):
                        logger.info("[DIAG] 纯OCR补充异常: entry[%d] pad=%.2f => %r", idx, pad, str(ocr_result)[:120])
                        continue
                    logger.info("[DIAG] 纯OCR返回: entry[%d] pad=%.2f => %r", idx, pad, (ocr_result or "")[:120])
                    if isinstance(ocr_result, str) and ocr_result and _has_spec_unit(ocr_result):
                        entries[idx]["ocr_text"] = ocr_result
                        logger.info("[DIAG] 纯OCR补充成功: entry[%d] pad=%.2f => %r", idx, pad, ocr_result[:80])

            # ── 最终兜底：底部聚焦裁剪。
            #    均匀放大裁剪在 padding 很大时会把整袋+背景一起送进去、
            #    净含量文字在图里占比太小导致 VLM 读不到。
            #    改为只切「框下方到估计袋底」这一条带，以更高有效分辨率呈现角落文字。
            pending_bottom = [i for i in ocr_supplement_indices if not _has_spec_unit(entries[i].get("ocr_text", ""))]
            if pending_bottom:
                bottom_crops = []
                for i in pending_bottom:
                    c = crop_bottom_focus(raw, entries[i]["box"])
                    bottom_crops.append(c)
                valid_bottom = [(i, c) for i, c in zip(pending_bottom, bottom_crops) if c]
                if valid_bottom:
                    bottom_results = await asyncio.gather(
                        *[vision.call_ocr_api(c, settings) for _, c in valid_bottom],
                        return_exceptions=True,
                    )
                    for (idx, _), ocr_result in zip(valid_bottom, bottom_results):
                        if isinstance(ocr_result, Exception):
                            logger.info("[DIAG] 底部OCR异常: entry[%d] => %r", idx, str(ocr_result)[:120])
                            continue
                        logger.info("[DIAG] 底部OCR返回: entry[%d] => %r", idx, (ocr_result or "")[:120])
                        if isinstance(ocr_result, str) and ocr_result and _has_spec_unit(ocr_result):
                            entries[idx]["ocr_text"] = ocr_result
                            logger.info("[DIAG] 底部OCR成功: entry[%d] => %r", idx, ocr_result[:80])

        # 整帧补读：YOLO 给出的候选框太少时，整张图再让大模型读一遍。
        # YOLO 漏掉的物体，整图视觉模型往往还能从全局看出多个商品，从而把
        # 「一张照片多个物品却只识别一件」的漏检补回来。整帧读出的商品没有
        # 精确坐标，box 记 None，聚合时用占位框均匀铺开，至少让用户看到全部商品。
        if settings.CASCADE_FULLFRAME_FALLBACK and len(boxes) <= settings.CASCADE_FULLFRAME_TRIGGER:
            try:
                full = await vision.call_vision_api(image_b64, settings, cropped=False)
            except BaseException:
                full = None
            if isinstance(full, list):
                for item in full:
                    if not isinstance(item, dict):
                        continue
                    cands = item.get("candidates")
                    if not isinstance(cands, list) or not cands:
                        continue
                    qty = max(1, int(item.get("quantity", 1) or 1))
                    ocr = str(item.get("ocr_text", "") or "").strip()
                    for c in cands:
                        if not isinstance(c, dict):
                            continue
                        entries.append(
                            {
                                "sku_id": str(c.get("sku_id", "")).strip(),
                                "confidence": c.get("confidence", 0.0),
                                "quantity": qty,
                                "ocr_text": ocr,
                                "box": None,
                                "measured_length_cm": None,
                            }
                        )
        return entries

    # 尺子比例尺探测（演示用）：每帧拍照时画面里放一把尺子，这里让视觉模型
    # 读一次尺子刻度，得到 px_per_cm。探测失败或没放尺子都返回 None，
    # 后续只跳过尺寸纠偏，不影响普通识别——尺子是增强项，不是依赖项。
    scale: float | None = None
    if settings.RULER_ENABLED and images:
        try:
            scale = await vision.probe_ruler_scale(images[0], settings)
        except Exception as exc:  # 探测失败绝不该拖垮结账链路
            logger.warning("尺子比例尺探测异常，跳过尺寸纠偏: %s", exc)
            scale = None
        # 记录尺子是否生效，便于排查「40g 被识别成 70g」：
        # scale 为 None 表示画面里没读到尺子，后续尺寸纠偏整段跳过、纯靠视觉（偏向 70g）。
        if scale:
            logger.info("尺子比例尺生效：px_per_cm=%.1f（真实尺长 %s cm 仅作对照）", scale, settings.RULER_LENGTH_CM)
        else:
            logger.warning("未检测到尺子比例尺，本次识别回退为纯视觉（同款规格无法靠尺子区分）")

    frame_entries = await asyncio.gather(*[process_frame(img, scale) for img in images])

    # —— 按框实例聚合（不再按 SKU 去重合并）——
    # 每个 YOLO 框 = 一个商品实例，独立成条；跨帧同一商品（位置相近）聚成一个实例簇。
    # 这样「一张照片里两包相同的薯片」会各自成条、数量各计 1，不再被同 SKU 合并吞掉一包；
    # 70g/40g 也因分属不同框而各自独立，不再被相似组合并强行合一条。
    instances = _cluster_instances(frame_entries)
    none_entries = [
        e for fe in frame_entries for e in fe if not isinstance(e.get("box"), BoxProposal)
    ]

    items: list[dict] = []
    boxes: list[tuple[float, float, float, float] | None] = []
    lengths: list[float | None] = []
    covered_skus: set[str] = set()

    for inst in instances:
        top_sku, cand_list, ocr, box, meas = _vote_instance(inst, products_by_sku)
        if not top_sku:
            continue
        covered_skus.add(top_sku)
        items.append({"candidates": cand_list, "quantity": 1, "ocr_text": ocr})
        boxes.append(box)
        lengths.append(meas)

    # 整帧补读（无坐标）：仅补充「有框实例没覆盖到的 SKU」，避免与已有行重复。
    # 它无法区分同款第二件（没有坐标），只负责补回 YOLO 漏掉的不同商品。
    if none_entries:
        extra_ordered, _ = vision.tally_votes([none_entries], products_by_sku)
        for sku, st in extra_ordered:
            if sku in covered_skus:
                continue
            cands = vision.build_voted_candidates(extra_ordered, 1, sku)
            ocr = vision.pick_ocr_text(st["ocrs"])
            items.append(
                {
                    "candidates": cands,
                    "quantity": vision.majority_quantity(st["qtys"]),
                    "ocr_text": ocr,
                }
            )
            boxes.append(None)
            lengths.append(None)
            covered_skus.add(sku)

    # 无坐标实例用占位框均匀铺开，保证它们也能在界面上呈现。
    none_idx = [i for i, b in enumerate(boxes) if b is None]
    if none_idx:
        placeholders = vision._placeholder_boxes(len(none_idx))
        for i, idx in enumerate(none_idx):
            boxes[idx] = placeholders[i]

    boxes = [b if b is not None else (10.0, 30.0, 18.0, 20.0) for b in boxes]

    detections = vision.build_detections(
        items, products_by_sku, boxes, settings, lengths=lengths, tray_weight_g=tray_weight_g,
    )
    # 级联完全没拿到任何商品时返回 None，让上层退回整图识别；绝不硬凑空结果。
    return detections if detections else None
