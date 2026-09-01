"""数据库表结构。

设计取舍：
- 商品主数据用宽表而不是拆成商品/营养/视觉多张表——这些字段始终一起读，
  拆表只会带来无谓的 JOIN，且导购查询的绝大多数场景是「按 SKU 取整行」。
- 促销独立成表，因为它有「全场」和「单品」两种作用域，是一对多关系。
- 检测结果的候选项、会话的确认记录这类结构化但不定长的数据用 JSON 列，
  避免为了它们再建两张子表。真到需要按候选项做统计分析时再拆。
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ProductModel(Base):
    __tablename__ = "products"

    sku_id = Column(String(32), primary_key=True)
    name = Column(String(128), nullable=False)
    brand = Column(String(64), index=True)
    category = Column(String(32), index=True)
    spec = Column(String(64))
    price = Column(Float, nullable=False)
    member_price = Column(Float, nullable=False)

    shelf_aisle = Column(String(16))
    shelf_level = Column(Integer)
    shelf_desc = Column(String(255))

    energy_kj = Column(Float, default=0)
    protein_g = Column(Float, default=0)
    fat_g = Column(Float, default=0)
    carb_g = Column(Float, default=0)
    sodium_mg = Column(Float, default=0)
    ingredients = Column(Text, default="")
    allergens = Column(JSON, default=list)
    tags = Column(JSON, default=list)

    visual_color = Column(String(16))
    visual_color2 = Column(String(16))
    visual_shape = Column(String(16))
    visual_label = Column(String(16))
    visual_weight_g = Column(Integer, default=0)
    # 包装外尺寸（实测铺平后的长宽，单位 cm）。用于「尺子比例尺」区分同款不同规格
    # （如乐事黄瓜味 70g / 40g），净含量之外的物理尺寸是更可靠的二选一依据。
    visual_pkg_width_cm = Column(Float, default=0.0)
    visual_pkg_length_cm = Column(Float, default=0.0)

    stock = Column(Integer, default=100)
    # 名称+品牌+规格+标签+配料的拼接副本，供关键词检索直接 LIKE，避免每次现拼
    search_text = Column(Text, default="")
    # 条形码（EAN-13 / EAN-8 / CODE-128 等），顾客在手机端扫码即跳到该商品。
    # 演示用 nullable：未贴条码的 SKU 留空，扫码接口会返回 404。
    barcode = Column(String(32), nullable=True)


Index("idx_product_category", ProductModel.category)
Index("idx_product_barcode", ProductModel.barcode)


class PromotionModel(Base):
    """促销规则。scope=store 表示全场，scope=sku 表示绑定单个商品。"""

    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(16), nullable=False, index=True)  # store | sku
    sku_id = Column(String(32), ForeignKey("products.sku_id"), nullable=True, index=True)
    type = Column(String(32), nullable=False)  # second_half | bundle | threshold
    desc = Column(String(128), nullable=False)
    bundle_price = Column(Float, nullable=True)
    threshold_amount = Column(Float, nullable=True)
    discount = Column(Float, nullable=True)
    groups = Column(JSON, nullable=True)  # bundle 专用：[[A组sku], [B组sku]]


class SimilarGroupModel(Base):
    """相似商品分组，供视觉仲裁判定歧义。同组商品共享一个 group_key。"""

    __tablename__ = "similar_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_key = Column(String(64), nullable=False, index=True)
    sku_id = Column(String(32), ForeignKey("products.sku_id"), nullable=False)


class CheckoutSessionModel(Base):
    __tablename__ = "checkout_sessions"

    session_id = Column(String(32), primary_key=True)
    state = Column(String(32), nullable=False, index=True)
    mode = Column(String(32), default="mock")
    member = Column(Boolean, default=True)
    tray_weight_g = Column(Float, nullable=True)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    paid_method = Column(String(32), nullable=True)
    resolved = Column(JSON, default=dict)      # box_id -> sku_id
    # 账单是从检测项派生出来的，但每次读取都重算会让 GET 请求变重，
    # 这里缓存一份快照，检测项或确认结果变化时由服务层负责刷新
    bill = Column(JSON, nullable=True)
    weight_check = Column(JSON, nullable=True)


class DetectionModel(Base):
    __tablename__ = "checkout_detections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(32), ForeignKey("checkout_sessions.session_id"), index=True)
    box_id = Column(String(32), nullable=False)
    x = Column(Float, default=0)
    y = Column(Float, default=0)
    w = Column(Float, default=0)
    h = Column(Float, default=0)
    quantity = Column(Integer, default=1)
    state = Column(String(16), default="auto")  # auto | clarify | review
    message = Column(Text, default="")
    candidates = Column(JSON, default=list)    # [{sku_id, name, score}]


class OrderModel(Base):
    __tablename__ = "orders"

    order_id = Column(String(32), primary_key=True)
    session_id = Column(String(32), nullable=False, index=True)
    total_quantity = Column(Integer, default=0)
    origin_amount = Column(Float, default=0)
    discount_amount = Column(Float, default=0)
    payable = Column(Float, default=0)
    status = Column(String(32), default="CONFIRMED", index=True)  # CONFIRMED | PAID
    pay_method = Column(String(32), nullable=True)
    created_at = Column(Float, nullable=False)
    paid_at = Column(Float, nullable=True)


class OrderItemModel(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(32), ForeignKey("orders.order_id"), index=True)
    sku_id = Column(String(32), nullable=False)
    name = Column(String(128), nullable=False)
    spec = Column(String(64))
    unit_price = Column(Float, default=0)
    quantity = Column(Integer, default=1)
    subtotal = Column(Float, default=0)
    discount = Column(Float, default=0)
    source = Column(String(16), default="auto")  # auto | clarify | manual
    confidence = Column(Float, default=1)
    promotions = Column(JSON, default=list)


class ChatLogModel(Base):
    """导购对话日志：每条「问 + 答」落 2 行（role=user/assistant），供后台看板展示。

    写入策略：失败降级（不阻塞主链路）。
    不存流式 delta 中间状态，只存最终聚合文本与去重后的 citations，避免行数爆炸。
    """

    __tablename__ = "chat_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=True, index=True)
    role = Column(String(16), nullable=False)  # user | assistant
    content = Column(Text, nullable=False, default="")
    citations = Column(JSON, default=list)
    mode = Column(String(16), default="llm")  # llm | local
    intent = Column(String(32), default="general")
    created_at = Column(Float, nullable=False)


Index("idx_chat_logs_created", ChatLogModel.created_at.desc())
Index("idx_chat_logs_session", ChatLogModel.session_id)


class AuditRecordModel(Base):
    """视觉结账的审计追溯记录：每张已支付订单配一张抓拍图与不可变账单快照。

    主要用途：
    - 防损：识别结果与最终账单对得上时取证，对不上时追责识别引擎 vs 人工复核的边界
    - 客诉：订单争议回查，能直接看到当时拍到的画面和顾客最终同意的清单

    设计取舍：
    - order_id 唯一约束 + FK 到 orders：审计记录不会被重复创建，也跟随订单生命周期
    - items_snapshot 用 JSON 落盘而不引用 order_items 表：商品改名、改价、下架后
      仍能拿到当时的购买清单；订单项增删不影响审计回溯
    - photo_path 允许为空：演示场景（demo:*）没有真实抓拍，不能伪造"拍过"
    - expires_at + 索引：清理脚本只命中这一列，不用扫全表
    """

    __tablename__ = "audit_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        String(32), ForeignKey("orders.order_id"), nullable=False, unique=True
    )
    session_id = Column(String(32), nullable=False)

    photo_path = Column(String(255), nullable=True)
    photo_sha256 = Column(String(16), default="")
    photo_size_bytes = Column(Integer, default=0)

    captured_at = Column(Float, nullable=False)
    items_snapshot = Column(JSON, default=list)
    bill_origin = Column(Float, default=0)
    bill_discount = Column(Float, default=0)
    bill_payable = Column(Float, default=0)
    pay_method = Column(String(32), nullable=True)
    recognize_mode = Column(String(32), default="")
    scene_id = Column(String(32), nullable=True)

    cleared = Column(Boolean, default=False, nullable=False, index=True)
    cleared_at = Column(Float, nullable=True)
    cleared_by = Column(String(64), nullable=True)
    clear_note = Column(Text, default="")

    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)


Index("idx_audit_session", AuditRecordModel.session_id)
Index("idx_audit_captured", AuditRecordModel.captured_at.desc())
Index("idx_audit_expires", AuditRecordModel.expires_at)

