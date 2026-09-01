from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Nutrition(BaseModel):
    """每 100g / 100ml 的营养成分。"""

    energy_kj: float
    protein_g: float
    fat_g: float
    carb_g: float
    sodium_mg: float


class ShelfLocation(BaseModel):
    aisle: str = Field(description="货架编号，如 A1")
    level: int = Field(description="层号，从地面起算 1 为最底层")
    desc: str = Field(description="给顾客看的方位描述")


class Promotion(BaseModel):
    type: str = Field(description="second_half | bundle | threshold | member")
    desc: str
    # second_half: 无附加参数
    # bundle: groups = [A组, B组]，从每组各取一件组成组合价 bundle_price
    # threshold: threshold_amount + discount
    # member: 用 member_price 计价
    with_skus: list[str] = Field(default_factory=list)
    groups: list[list[str]] = Field(default_factory=list)
    bundle_price: float | None = None
    threshold_amount: float | None = None
    discount: float | None = None


class VisualProfile(BaseModel):
    """前端绘制商品示意图所需的视觉特征，无外部图片依赖。"""

    color: str
    color2: str = "#FFFFFF"
    shape: str = Field(description="can | bottle | box | bag | tube | pack | cup")
    label: str = Field(description="图上显示的短标签")
    weight_g: int = Field(description="单件标称重量，用于重量校验")
    # 包装外尺寸（实测铺平后的长宽，单位 cm）。用于「尺子比例尺」区分同款不同规格，
    # 例如乐事黄瓜味薯片 70g 与 40g 外观几乎一致，靠净含量之外的物理尺寸来二选一。
    # 未测量时填 0，识别逻辑会跳过尺寸纠偏、不强行参与判断。
    pkg_width_cm: float = 0.0
    pkg_length_cm: float = 0.0


class Product(BaseModel):
    sku_id: str
    name: str
    brand: str
    category: str
    spec: str
    price: float
    member_price: float
    shelf: ShelfLocation
    nutrition: Nutrition
    ingredients: str
    allergens: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    promotions: list[Promotion] = Field(default_factory=list)
    visual: VisualProfile
    stock: int = 100
    # 条形码：nullable，未贴条码的商品留空；扫码接口会返回 404
    barcode: str | None = None


# ---------------- 导购 Agent ----------------


class Citation(BaseModel):
    sku_id: str
    name: str
    reason: str = ""


class AskRequest(BaseModel):
    session_id: str | None = None
    question: str
    history: list[dict[str, str]] = Field(default_factory=list)
    use_llm: bool = Field(default=True, description="False 时强制走本地知识库")


class AskMeta(BaseModel):
    mode: str = Field(description="llm | local")
    intent: str = ""
    latency_ms: int = 0


# ---------------- 视觉结账 ----------------


class Candidate(BaseModel):
    sku_id: str
    name: str
    score: float


class Detection(BaseModel):
    """一个被识别出的目标框，坐标是相对画面区域的百分比。"""

    box_id: str
    x: float
    y: float
    w: float
    h: float
    candidates: list[Candidate]
    quantity: int = 1
    state: str = Field(description="auto | clarify | review")
    message: str = ""
    evidence: str | None = Field(
        default=None,
        description="识别依据，通常是模型从包装上读到的文字（品牌/净含量/规格）",
    )


class RecognizeRequest(BaseModel):
    scene_id: str | None = Field(default=None, description="示例购物盘场景 ID")
    image_base64: str | None = Field(default=None, description="真实拍照上传")
    images: list[str] | None = Field(
        default=None,
        description="连拍多帧（base64 列表），开启多帧投票时优先于 image_base64",
    )
    tray_weight_g: float | None = Field(default=None, description="称重传感器读数")
    use_vision_model: bool = True
    append: bool = Field(
        default=False,
        description="逐件录入模式：将本次识别结果追加到已有清单，而非整体替换（不清空已确认项）",
    )


class RecognizeResponse(BaseModel):
    session_id: str
    mode: str = Field(
        description="vision | vision-vote | vision-cascade:<检测器> | vision-failed | demo:<场景>"
    )
    detections: list[Detection]
    need_clarify: bool
    need_review: bool
    weight_check: dict[str, Any] | None = None
    elapsed_ms: int
    frames: int = Field(default=1, description="本次参与投票的帧数，1 表示单帧识别")


class ClarifyRequest(BaseModel):
    box_id: str
    sku_id: str


class CheckoutItem(BaseModel):
    sku_id: str
    name: str
    spec: str
    unit_price: float
    quantity: int
    subtotal: float
    discount: float = 0.0
    source: str = Field(description="auto | clarify | manual")
    confidence: float
    promotions: list[str] = Field(default_factory=list)


class Bill(BaseModel):
    session_id: str
    items: list[CheckoutItem]
    total_quantity: int
    origin_amount: float
    discount_amount: float
    payable: float


class SessionState(str, Enum):
    CREATED = "CREATED"
    CAPTURING = "CAPTURING"
    RECOGNIZING = "RECOGNIZING"
    NEED_CLARIFY = "NEED_CLARIFY"
    REVIEWING = "REVIEWING"
    CONFIRMED = "CONFIRMED"
    PAYING = "PAYING"
    PAID = "PAID"
    FAILED = "FAILED"


class SessionSnapshot(BaseModel):
    session_id: str
    state: SessionState
    detections: list[Detection] = Field(default_factory=list)
    bill: Bill | None = None
    mode: str = "mock"
    created_at: float
    resolved: dict[str, str] = Field(default_factory=dict, description="box_id -> 用户确认的 sku_id")
    member: bool = True
    tray_weight_g: float | None = None
    weight_check: dict[str, Any] | None = None
    paid_method: str | None = None


class PayRequest(BaseModel):
    method: str = Field(default="wechat", description="wechat | alipay | face")


class ManualItemRequest(BaseModel):
    """识别失败或漏检时，由顾客/店员手动录入一件商品。

    与 clarify 的区别：clarify 必须先有检测框，而识别整体失败时画面里
    一个框都没有，得允许凭空补录，否则顾客拿着商品却结不了账。
    """

    sku_id: str
    quantity: int = Field(default=1, ge=1, le=99)


class QuantityRequest(BaseModel):
    """逐件录入场景下调整某条目数量：同款多件无需反复拍摄。

    delta 为变化量，+1 表示增加一件，-1 表示减少一件；结果不低于 1。
    """

    delta: int = Field(description="数量变化量，+1 增加一件，-1 减少一件（不低于 1）")
