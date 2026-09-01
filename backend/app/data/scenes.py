"""示例购物盘场景（模拟识别模式的输入源）。

四个场景刻意覆盖四种典型情况：
  S1 常规采购   —— 全部高置信，含多件同品，演示顺畅路径与第二件半价
  S2 相似包装   —— 两个相似品对，演示置信度不足时的主动追问
  S3 拥挤遮挡   —— 一件商品被压住，演示转人工复核 + 重量校验发现漏检
  S4 新商品测试 —— 识别测试新增的 4 款商品，其中两件为同品不同规格，考验细粒度区分

坐标均为相对购物盘画面的百分比，前端据此叠加检测框。
"""

from pydantic import BaseModel, Field


class SceneItem(BaseModel):
    sku_id: str
    x: float
    y: float
    w: float
    h: float
    rotate: float = 0.0
    quantity: int = 1
    base_score: float = 0.95
    ambiguous_with: list[str] = Field(default_factory=list)
    occluded: bool = False


class Scene(BaseModel):
    scene_id: str
    title: str
    desc: str
    tray_weight_g: float
    items: list[SceneItem]


SCENES: list[Scene] = [
    Scene(
        scene_id="S1",
        title="日常小采购",
        desc="3 种商品、共 4 件，包装差异明显，识别无歧义",
        tray_weight_g=1355.0,
        items=[
            SceneItem(sku_id="SKU001", x=16, y=34, w=30, h=22, rotate=-6, quantity=2, base_score=0.96),
            SceneItem(sku_id="SKU005", x=52, y=28, w=13, h=30, rotate=4, base_score=0.95),
            SceneItem(sku_id="SKU009", x=70, y=42, w=24, h=18, rotate=-3, base_score=0.93),
        ],
    ),
    Scene(
        scene_id="S2",
        title="相似包装难题",
        desc="两对同品牌不同口味商品，视觉特征接近，需向顾客追问确认",
        tray_weight_g=700.0,
        items=[
            SceneItem(
                sku_id="SKU004", x=18, y=28, w=14, h=30, rotate=5,
                base_score=0.62, ambiguous_with=["SKU003"],
            ),
            SceneItem(
                sku_id="SKU010", x=40, y=42, w=24, h=18, rotate=-4,
                base_score=0.68, ambiguous_with=["SKU009"],
            ),
            SceneItem(sku_id="SKU011", x=70, y=34, w=18, h=24, rotate=2, base_score=0.94),
        ],
    ),
    Scene(
        scene_id="S3",
        title="拥挤与遮挡",
        desc="6 件商品堆叠，一件被压住无法识别，转人工复核；称重同时发现漏检",
        tray_weight_g=865.0,
        items=[
            SceneItem(sku_id="SKU007", x=10, y=32, w=13, h=22, rotate=-8, base_score=0.96),
            SceneItem(sku_id="SKU012", x=26, y=38, w=17, h=17, rotate=3, base_score=0.91),
            SceneItem(sku_id="SKU015", x=46, y=44, w=17, h=12, rotate=-2, base_score=0.90),
            SceneItem(sku_id="SKU013", x=66, y=30, w=17, h=22, rotate=6, base_score=0.88),
            SceneItem(sku_id="SKU014", x=24, y=60, w=18, h=14, rotate=-5, base_score=0.86),
            SceneItem(sku_id="SKU017", x=48, y=60, w=15, h=13, rotate=9, base_score=0.41, occluded=True),
        ],
    ),
    Scene(
        scene_id="S4",
        title="新商品识别测试",
        desc="4 款新增商品：水溶C100 与崂山啤酒高置信入账；乐事 40g、奥利奥 97g 与同品不同规格的旧 SKU 混淆，触发追问",
        tray_weight_g=1420.0,
        items=[
            SceneItem(sku_id="SKU026", x=14, y=26, w=13, h=32, rotate=-5, base_score=0.95),
            SceneItem(sku_id="SKU028", x=34, y=24, w=12, h=34, rotate=6, base_score=0.93),
            SceneItem(
                sku_id="SKU027", x=56, y=34, w=20, h=15, rotate=-3,
                base_score=0.70, ambiguous_with=["SKU010"],
            ),
            SceneItem(
                sku_id="SKU025", x=76, y=32, w=18, h=24, rotate=2,
                base_score=0.66, ambiguous_with=["SKU011"],
            ),
        ],
    ),
]

SCENE_MAP: dict[str, Scene] = {s.scene_id: s for s in SCENES}
