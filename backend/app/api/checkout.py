import asyncio
import time

from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.data.scenes import SCENES
from app.db.database import session_scope
from app.db.models import OrderItemModel, OrderModel
from app.models.schemas import (
    Bill,
    ClarifyRequest,
    Detection,
    ManualItemRequest,
    PayRequest,
    QuantityRequest,
    RecognizeRequest,
    RecognizeResponse,
    SessionSnapshot,
    SessionState,
)
from app.repositories import product_repo
from app.services import audit, vision
from app.services.arbitration import to_bill_lines, weight_check
from app.services.pricing import build_bill
from app.services.store import session_store

router = APIRouter()


def _get_session(session_id: str) -> SessionSnapshot:
    snapshot = session_store.get(session_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"结账会话不存在: {session_id}")
    return snapshot


def _rebuild_bill(snapshot: SessionSnapshot) -> Bill:
    lines = to_bill_lines(snapshot.detections, snapshot.resolved)
    return build_bill(snapshot.session_id, lines, member=snapshot.member)


def _refresh_bill_and_weight(snapshot: SessionSnapshot) -> None:
    """账单与称重校验必须同步刷新，否则人工补入商品后仍会提示漏检。"""
    snapshot.bill = _rebuild_bill(snapshot)
    snapshot.weight_check = weight_check(
        snapshot.detections,
        snapshot.tray_weight_g,
        get_settings(),
        snapshot.resolved,
    )


def _refresh_state(snapshot: SessionSnapshot) -> None:
    has_clarify = any(d.state == "clarify" and d.box_id not in snapshot.resolved for d in snapshot.detections)
    has_review = any(d.state == "review" and d.box_id not in snapshot.resolved for d in snapshot.detections)
    if has_clarify:
        snapshot.state = SessionState.NEED_CLARIFY
    elif has_review:
        snapshot.state = SessionState.REVIEWING
    else:
        snapshot.state = SessionState.CONFIRMED


@router.get("/checkout/scenes")
async def list_scenes() -> list[dict]:
    return [
        {
            "scene_id": s.scene_id,
            "title": s.title,
            "desc": s.desc,
            "item_count": sum(i.quantity for i in s.items),
            "tray_weight_g": s.tray_weight_g,
        }
        for s in SCENES
    ]


@router.post("/checkout/sessions")
async def create_session() -> SessionSnapshot:
    return session_store.create()


@router.get("/checkout/sessions/{session_id}")
async def get_session(session_id: str) -> SessionSnapshot:
    return _get_session(session_id)


@router.post("/checkout/sessions/{session_id}/recognize")
async def recognize(session_id: str, request: RecognizeRequest) -> RecognizeResponse:
    settings = get_settings()
    snapshot = _get_session(session_id)

    snapshot.state = SessionState.CAPTURING
    # 逐件录入模式：保留已确认项与既有清单，只把本次结果追加进去；
    # 整盘识别模式：每次都是一次全新的识别，清空上一轮的 resolved。
    if not request.append:
        snapshot.resolved = {}
        snapshot.tray_weight_g = request.tray_weight_g

    new_detections, mode, elapsed = await vision.recognize(
        scene_id=request.scene_id,
        image_base64=request.image_base64,
        use_vision_model=request.use_vision_model,
        settings=settings,
        images=request.images,
        tray_weight_g=request.tray_weight_g,
    )
    frames = len([f for f in (request.images or []) if f]) or (1 if request.image_base64 else 0)

    if request.append:
        # 模型每次识别都从 V1 起编，直接追加会和既有 V1/V2 撞号，
        # 必须按当前清单长度做全局重排，否则 clarify/manual/remove 都会指错框。
        offset = len(snapshot.detections)
        for i, d in enumerate(new_detections):
            d.box_id = f"V{offset + i + 1}"
        snapshot.detections = snapshot.detections + new_detections
    else:
        snapshot.detections = new_detections

    # 审计追溯：识别一收到首帧就先把它抓到 _pending/，pay 成功时再迁正式目录。
    # 写盘失败降级（不抛错），保障主链路不被审计拖累。
    if not mode.startswith("demo:") and (request.images or request.image_base64):
        first_frame = next((f for f in (request.images or []) if f), None) or request.image_base64
        if first_frame:
            audit.save_pending(session_id, first_frame, settings)

    snapshot.state = SessionState.RECOGNIZING
    snapshot.mode = mode
    snapshot.weight_check = weight_check(
        snapshot.detections,
        snapshot.tray_weight_g,
        settings,
        snapshot.resolved,
    )
    snapshot.bill = _rebuild_bill(snapshot)
    _refresh_state(snapshot)
    session_store.save(snapshot)

    return RecognizeResponse(
        session_id=session_id,
        mode=mode,
        detections=snapshot.detections,
        need_clarify=any(d.state == "clarify" for d in snapshot.detections),
        need_review=any(d.state == "review" for d in snapshot.detections),
        weight_check=snapshot.weight_check,
        elapsed_ms=elapsed,
        frames=frames,
    )


@router.post("/checkout/sessions/{session_id}/clarify")
async def clarify(session_id: str, request: ClarifyRequest) -> SessionSnapshot:
    snapshot = _get_session(session_id)
    target = next((d for d in snapshot.detections if d.box_id == request.box_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"检测框不存在: {request.box_id}")

    if target.state == "review":
        # 已转人工的项目允许指定任意在售商品：模型连候选都给不准时，得让人来定
        if product_repo.get_product(request.sku_id) is None:
            raise HTTPException(status_code=400, detail=f"商品不存在: {request.sku_id}")
    elif not any(c.sku_id == request.sku_id for c in target.candidates):
        raise HTTPException(status_code=400, detail="所选商品不在候选项内")

    snapshot.resolved[request.box_id] = request.sku_id
    _refresh_bill_and_weight(snapshot)
    _refresh_state(snapshot)
    session_store.save(snapshot)
    return snapshot


@router.post("/checkout/sessions/{session_id}/manual")
async def add_manual_item(session_id: str, request: ManualItemRequest) -> SessionSnapshot:
    """识别失败或漏检时，手动补录一件商品。

    与 clarify 的关键区别：clarify 要求先有检测框，而识别整体失败时画面里
    一个框都没有。若仍要求从检测框里选，顾客拿着商品却结不了账。
    """
    snapshot = _get_session(session_id)
    if product_repo.get_product(request.sku_id) is None:
        raise HTTPException(status_code=400, detail=f"商品不存在: {request.sku_id}")

    seq = sum(1 for d in snapshot.detections if d.box_id.startswith("M")) + 1
    box_id = f"M{seq}"
    snapshot.detections.append(
        Detection(
            box_id=box_id,
            x=0,
            y=0,
            w=0,
            h=0,
            candidates=[],
            quantity=request.quantity,
            state="review",
            message="人工录入",
        )
    )
    snapshot.resolved[box_id] = request.sku_id
    _refresh_bill_and_weight(snapshot)
    _refresh_state(snapshot)
    session_store.save(snapshot)
    return snapshot


@router.delete("/checkout/sessions/{session_id}/items/{box_id}")
async def remove_item(session_id: str, box_id: str) -> SessionSnapshot:
    """移除一笔检测项（含人工补录与待确认项）。

    检测器偶发的重复框、或顾客误选会导致账单里出现不该有的条目，
    之前没有任何删除入口，只能整单重置。这里允许按 box_id 精确摘除，
    并同步刷新账单、称重校验与会话状态。
    """
    snapshot = _get_session(session_id)
    target = next((d for d in snapshot.detections if d.box_id == box_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"检测框不存在: {box_id}")

    snapshot.detections = [d for d in snapshot.detections if d.box_id != box_id]
    snapshot.resolved.pop(box_id, None)
    _refresh_bill_and_weight(snapshot)
    _refresh_state(snapshot)
    session_store.save(snapshot)
    return snapshot


@router.patch("/checkout/sessions/{session_id}/items/{box_id}/quantity")
async def adjust_quantity(session_id: str, box_id: str, request: QuantityRequest) -> SessionSnapshot:
    """调整某条目数量（同款多件时不必反复拍摄录入）。

    仅允许对「已确认商品」的条目操作：auto/clarify 已 resolved，或 review 被人工确认。
    未确认（无候选也无 resolved）的条目无法定商品，自然不能调数量。
    """
    snapshot = _get_session(session_id)
    target = next((d for d in snapshot.detections if d.box_id == box_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"检测框不存在: {box_id}")

    chosen = snapshot.resolved.get(box_id) or (
        target.candidates[0].sku_id if target.candidates else None
    )
    if not chosen:
        raise HTTPException(status_code=400, detail="该条目尚未确认商品，无法调整数量")

    new_qty = max(1, target.quantity + request.delta)
    if new_qty == target.quantity:
        return snapshot  # 已是下限 1，无需改动
    target.quantity = new_qty

    _refresh_bill_and_weight(snapshot)
    _refresh_state(snapshot)
    session_store.save(snapshot)
    return snapshot


@router.post("/checkout/sessions/{session_id}/confirm")
async def confirm(session_id: str, member: bool = True) -> SessionSnapshot:
    """顾客确认账单无误，锁定金额进入支付。

    未解决的复核项（含「识别到商品但无法确定品类」的空候选项）不能带着进支付：
    to_bill_lines 会直接跳过它们，导致未识别商品被静默放过、账单缺漏。
    这里在确认前强制拦截，要求先人工复核或移除。
    """
    snapshot = _get_session(session_id)
    if not snapshot.detections:
        raise HTTPException(status_code=400, detail="尚未识别任何商品")

    unresolved = [
        d for d in snapshot.detections
        if d.state == "review" and d.box_id not in snapshot.resolved
    ]
    if unresolved:
        n_unknown = sum(1 for d in unresolved if not d.candidates)
        tail = f"（其中 {n_unknown} 件未识别到品类）" if n_unknown else ""
        raise HTTPException(
            status_code=400,
            detail=f"还有 {len(unresolved)} 件商品未确认{tail}，请先在复核区指认或移除后再支付",
        )

    snapshot.member = member
    _refresh_bill_and_weight(snapshot)
    snapshot.state = SessionState.CONFIRMED
    session_store.save(snapshot)
    return snapshot


@router.post("/checkout/sessions/{session_id}/pay")
async def pay(session_id: str, request: PayRequest) -> SessionSnapshot:
    snapshot = _get_session(session_id)
    if snapshot.state not in (SessionState.CONFIRMED, SessionState.PAYING):
        raise HTTPException(status_code=400, detail="账单尚未确认，无法支付")
    if snapshot.bill is None or snapshot.bill.payable <= 0:
        raise HTTPException(status_code=400, detail="账单为空，无法支付")

    # 库存校验放在扣款之前：不能让顾客付完钱才发现货不够
    needed: dict[str, int] = {}
    for item in snapshot.bill.items:
        needed[item.sku_id] = needed.get(item.sku_id, 0) + item.quantity
    problems = product_repo.check_stock(needed)
    if problems:
        raise HTTPException(status_code=409, detail="；".join(problems))

    snapshot.state = SessionState.PAYING
    session_store.save(snapshot)
    await asyncio.sleep(1.2)  # 模拟支付网关往返

    snapshot.state = SessionState.PAID
    snapshot.paid_method = request.method
    session_store.save(snapshot)
    _persist_order(snapshot)
    return snapshot


def _persist_order(snapshot: SessionSnapshot) -> None:
    """支付完成后落订单。重复调用不会产生重复订单（以 session_id 作主键）。"""
    bill = snapshot.bill
    if bill is None or not bill.items:
        return

    with session_scope() as db:
        order = db.get(OrderModel, snapshot.session_id)
        if order is None:
            order = OrderModel(
                order_id=snapshot.session_id,
                session_id=snapshot.session_id,
                created_at=snapshot.created_at,
            )
            db.add(order)
            db.flush()

            for item in bill.items:
                db.add(
                    OrderItemModel(
                        order_id=snapshot.session_id,
                        sku_id=item.sku_id,
                        name=item.name,
                        spec=item.spec,
                        unit_price=item.unit_price,
                        quantity=item.quantity,
                        subtotal=item.subtotal,
                        discount=item.discount,
                        source=item.source,
                        confidence=item.confidence,
                        promotions=list(item.promotions),
                    )
                )

            # 库存扣减与订单落库同事务、同幂等条件：
            # 只有首次创建订单时扣一次，重复支付/重试不会重复扣
            product_repo.deduct_stock(db, _bill_quantities(bill))

        order.total_quantity = bill.total_quantity
        order.origin_amount = bill.origin_amount
        order.discount_amount = bill.discount_amount
        order.payable = bill.payable
        order.status = "PAID"
        order.pay_method = snapshot.paid_method
        order.paid_at = time.time()

        # 审计追溯：把 _pending/ 下的抓拍图迁到正式目录，同事务写 audit_records 一行。
        # demo:* 场景没有真实抓拍，依然写占位记录（photo_path 留空）。
        # autoflush 默认开，但 query 不会自动 flush 已 add 的行，必须显式 flush 再读，
        # 否则 items_snapshot 会是空数组。
        db.flush()
        items_iter = (
            db.query(OrderItemModel)
            .filter(OrderItemModel.order_id == snapshot.session_id)
            .all()
        )
        audit.promote_and_persist(
            db=db,
            settings=get_settings(),
            snapshot=snapshot,
            bill=bill,
            order=order,
            recognized_mode=snapshot.mode,
            scene_id=_scene_id_for_audit(snapshot),
            recognized_at=snapshot.created_at,
            items_iter=items_iter,
        )


def _scene_id_for_audit(snapshot) -> str | None:
    """从 snapshot 里抽出场景 ID；recognize 整体失败时可能为空。"""
    return getattr(snapshot, "scene_id", None) if hasattr(snapshot, "scene_id") else None


def _bill_quantities(bill: Bill) -> dict[str, int]:
    """账单条目聚合成 sku -> 总数量，供库存校验与扣减共用。"""
    quantities: dict[str, int] = {}
    for item in bill.items:
        quantities[item.sku_id] = quantities.get(item.sku_id, 0) + item.quantity
    return quantities


@router.post("/checkout/sessions/{session_id}/reset")
async def reset(session_id: str) -> SessionSnapshot:
    snapshot = _get_session(session_id)
    snapshot.state = SessionState.CREATED
    snapshot.detections = []
    snapshot.resolved = {}
    snapshot.bill = None
    snapshot.weight_check = None
    snapshot.tray_weight_g = None
    snapshot.created_at = time.time()
    session_store.save(snapshot)
    return snapshot
