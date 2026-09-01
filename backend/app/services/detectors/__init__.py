"""检测器实现集合。

新增检测器只需两步：实现 base.Detector 协议，再在 get_detector 里登记。
上层 cascade 与前端不需要任何改动。
"""

from app.services.detectors.base import BoxProposal, Detector
from app.services.detectors.yolo import YoloCocoDetector

__all__ = ["BoxProposal", "Detector", "YoloCocoDetector"]
