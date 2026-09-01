"""COCO 预训练 YOLO 做区域提议。

为什么只取框、不取类别：
    COCO 的 80 个类里没有「薯片袋」「饼干盒」「卷装饼干」这类零售包装，
    只有 bottle / book / handbag 等勉强沾边的通用类别。指望它告诉你
    「这是乐事 40g」不现实，但它确实能告诉你「画面这个位置有个独立物体」。
    所以这里一律丢弃 cls 字段，只把框交给视觉大模型去认。

为什么可以零训练接入：
    检测框与类别判断解耦后，检测器只要能稳定圈出物体就有价值，
    不必认识具体 SKU。这是本项目在「没有自采训练数据」阶段的过渡方案，
    最终仍应换成 training/ 工作区微调出的 best.pt——届时只需换权重路径。
"""

from __future__ import annotations

import io
import logging
import threading

from app.services.detectors.base import BoxProposal

logger = logging.getLogger(__name__)

# 模型权重加载一次即可，进程内共享。加锁是因为 FastAPI 的并发请求
# 可能同时触发首次加载，重复构造 YOLO 会白白吃掉几百毫秒和显存/内存。
_cache: dict[str, object] = {}
_cache_lock = threading.Lock()

# 面积占比过滤：太小的多半是包装褶皱或噪点，
# 太大的通常把整个购物盘都框进去了——那对定位单个商品没有意义。
MIN_AREA_RATIO = 0.01
MAX_AREA_RATIO = 0.60


def _iou(a: "BoxProposal", b: "BoxProposal") -> float:
    """交并比。坐标是相对百分比，面积比直接拿百分比算，单位一致即可。"""
    ax2, ay2 = a.x + a.w, a.y + a.h
    bx2, by2 = b.x + b.w, b.y + b.h
    ix1, iy1 = max(a.x, b.x), max(a.y, b.y)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _contains(big: "BoxProposal", small: "BoxProposal", ratio: float = 0.85) -> bool:
    """small 是否基本落在 big 内部——应对「一个框套着另一个框」的重复提议。"""
    inter = max(0.0, min(big.x + big.w, small.x + small.w) - max(big.x, small.x)) * max(
        0.0, min(big.y + big.h, small.y + small.h) - max(big.y, small.y)
    )
    return small.w * small.h > 0 and inter / (small.w * small.h) >= ratio


def suppress_overlaps(
    boxes: list["BoxProposal"], iou_threshold: float
) -> list["BoxProposal"]:
    """跨类非极大值抑制：一个物理物体只留一个框。

    ultralytics 的 predict 已按 COCO 类各做一遍 NMS，但同一物体被分到
    不同类时跨类不合并，会漏出多个重叠框。这里丢弃类别后统一抑制：
    高分框保留，与之重叠（IoU 超阈）或包含它的低分框压掉。
    """
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda p: -p.score)
    kept: list["BoxProposal"] = []
    for b in ordered:
        redundant = False
        for k in kept:
            if _iou(k, b) >= iou_threshold or _contains(k, b) or _contains(b, k):
                redundant = True
                break
        if not redundant:
            kept.append(b)
    return kept


class YoloCocoDetector:
    """用 COCO 预训练权重提候选框。

    构造时不加载模型——DETECTOR=off 的场景下不该为一个用不到的模型
    付出启动时间和内存。
    """

    def __init__(
        self,
        weights: str,
        conf: float = 0.15,
        imgsz: int = 640,
        max_boxes: int = 8,
        nms_iou: float = 0.55,
    ) -> None:
        self.weights = weights
        self.conf = conf
        self.imgsz = imgsz
        self.max_boxes = max_boxes
        self.nms_iou = nms_iou
        self._model = None

    @property
    def name(self) -> str:
        return "yolo-coco"

    def _load(self):
        if self._model is not None:
            return self._model
        with _cache_lock:
            if self.weights in _cache:
                self._model = _cache[self.weights]
                return self._model
            try:
                from ultralytics import YOLO
            except ImportError as exc:  # pragma: no cover - 依赖缺失时的降级路径
                raise RuntimeError(f"未安装 ultralytics，无法启用 YOLO 检测: {exc}") from exc

            logger.info("加载 YOLO 权重: %s", self.weights)
            model = YOLO(self.weights)
            _cache[self.weights] = model
            self._model = model
            return model

    def warmup(self) -> None:
        """提前加载权重，让配置错误在启动时暴露而不是在顾客结账时暴露。"""
        self._load()

    def propose(self, image_bytes: bytes) -> list[BoxProposal]:
        from PIL import Image

        model = self._load()
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = img.size

        results = model.predict(
            img,
            conf=self.conf,
            imgsz=self.imgsz,
            device="cpu",
            verbose=False,
        )

        proposals: list[BoxProposal] = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            w_px = max(x2 - x1, 0.0)
            h_px = max(y2 - y1, 0.0)
            area_ratio = (w_px * h_px) / max(width * height, 1)
            if area_ratio < MIN_AREA_RATIO or area_ratio > MAX_AREA_RATIO:
                continue

            proposals.append(
                BoxProposal(
                    x=round(x1 / width * 100, 2),
                    y=round(y1 / height * 100, 2),
                    w=round(w_px / width * 100, 2),
                    h=round(h_px / height * 100, 2),
                    score=round(float(box.conf), 3),
                )
            )

        # 跨类 NMS：同物体被 YOLO 框成多个重叠框时只留一个，
        # 否则一个盒子会变成两件独立商品、顾客被重复计费。
        proposals = suppress_overlaps(proposals, self.nms_iou)

        # 按检测器把握排序后截断：一张图里若冒出几十个框，逐个送给大模型
        # 会让响应时间失控，而低分框多半是误检，本来也不该采信。
        proposals.sort(key=lambda p: -p.score)
        return proposals[: self.max_boxes]
