"""检测器抽象层。

定义这层的目的只有一个：让「用哪个模型出框」成为可替换的实现细节，
而不是散落在识别流程里的分支。视觉大模型、COCO 预训练 YOLO、将来微调的
best.pt，只要都实现 propose()，上层 cascade 与前端就一行都不用改。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class BoxProposal(BaseModel):
    """一个候选框，坐标与 Detection 一致，是相对画面的百分比。

    刻意不携带类别名：本项目的检测器只负责「这里有个东西」，
    判断它是什么交给视觉大模型。检测器给出的类别名对零售商品没有参考价值
    （COCO 里根本没有薯片袋、饼干盒），带着反而会误导下游。
    """

    x: float = Field(description="左上角横向位置（百分比）")
    y: float = Field(description="左上角纵向位置（百分比）")
    w: float = Field(description="框宽度（百分比）")
    h: float = Field(description="框高度（百分比）")
    score: float = Field(default=0.0, description="检测器对「这里有物体」的把握")


@runtime_checkable
class Detector(Protocol):
    """检测器协议：给定一张图，返回若干候选框。"""

    @property
    def name(self) -> str:
        """用于日志与模式标识，如 yolo-coco、yolo-finetuned。"""
        ...

    def propose(self, image_bytes: bytes) -> list[BoxProposal]:
        """同步接口。实现方若封装的是同步模型（ultralytics 就是），
        由调用方放进线程池执行，避免阻塞事件循环。
        """
        ...
