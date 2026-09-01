"""后台看板接口：今日实时 / 热销 TOP10 / 库存预警 / 销售趋势 / 最近对话。

鉴权：默认开放（dev 演示用）。env 里设 ADMIN_TOKEN=xxx 开启 Bearer 校验。
SQL 聚合：SQLite 时间字段是 unix epoch float，聚合按 day 用
``strftime('%Y-%m-%d', paid_at, 'unixepoch', 'localtime')``；演示数据库量级
（几百订单）以下加索引即可秒回。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.database import get_session_factory
from app.db.models import (
    AuditRecordModel,
    ChatLogModel,
    OrderItemModel,
    OrderModel,
    ProductModel,
)
from app.services import audit as audit_service

router = APIRouter()
_logger = logging.getLogger("smartmart.admin")


# ---------------------------------------------------------------- 鉴权依赖
def _require_admin(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    """校验 Bearer token。ADMIN_TOKEN 为空时直接放行。"""
    expected = (settings.ADMIN_TOKEN or "").strip()
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    if provided != expected:
        raise HTTPException(status_code=403, detail="token 不正确")


def _db() -> Session:
    """每次请求一个 session，用完关。"""
    return get_session_factory()()


def _day_start_local(ts: float) -> float:
    """今天 0 点的本地时间戳（按 server localtime）。"""
    local = datetime.fromtimestamp(ts)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ---------------------------------------------------------------- 接口 1：KPI 总览


@router.get("/admin/overview", dependencies=[Depends(_require_admin)])
async def overview(
    days: int = Query(default=1, ge=1, le=30, description="统计最近 N 天"),
) -> dict[str, Any]:
    """KPI 总览：订单数 / 销售额 / 客单价 / 售出件数 / 独立顾客。"""
    now = time.time()
    since = _day_start_local(now) - max(days - 1, 0) * 86400

    with _db() as db:
        order_row = db.execute(
            select(
                func.count(OrderModel.order_id),
                func.coalesce(func.sum(OrderModel.payable), 0.0),
                func.coalesce(func.sum(OrderModel.total_quantity), 0),
                func.count(func.distinct(OrderModel.session_id)),
            ).where(OrderModel.status == "PAID", OrderModel.paid_at >= since)
        ).one()
        orders, revenue, items_sold, customers = order_row

        # 昨日同期同窗口对比
        prev_since = since - days * 86400
        prev_row = db.execute(
            select(
                func.count(OrderModel.order_id),
                func.coalesce(func.sum(OrderModel.payable), 0.0),
            ).where(
                OrderModel.status == "PAID",
                OrderModel.paid_at >= prev_since,
                OrderModel.paid_at < since,
            )
        ).one()
        prev_orders, prev_revenue = prev_row

    avg_basket = round(revenue / orders, 2) if orders else 0.0

    def pct(curr: float, prev: float) -> float | None:
        if prev <= 0:
            return None if curr <= 0 else 100.0
        return round((curr - prev) / prev * 100, 1)

    return {
        "window_days": days,
        "orders": int(orders),
        "revenue": round(float(revenue), 2),
        "avg_basket": avg_basket,
        "items_sold": int(items_sold),
        "customers": int(customers),
        "delta": {
            "orders_pct": pct(orders or 0, prev_orders or 0),
            "revenue_pct": pct(revenue or 0, prev_revenue or 0),
        },
    }


# ---------------------------------------------------------------- 接口 2：热销 TOP N


@router.get("/admin/bestsellers", dependencies=[Depends(_require_admin)])
async def bestsellers(
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[dict[str, Any]]:
    """热销 TOP N：按窗口内 SUM(quantity) 降序，附销售额。"""
    since = _day_start_local(time.time()) - max(days - 1, 0) * 86400
    with _db() as db:
        rows = db.execute(
            select(
                OrderItemModel.sku_id,
                OrderItemModel.name,
                func.coalesce(func.sum(OrderItemModel.quantity), 0).label("qty"),
                func.coalesce(func.sum(OrderItemModel.subtotal), 0.0).label("revenue"),
            )
            .join(OrderModel, OrderModel.order_id == OrderItemModel.order_id)
            .where(OrderModel.status == "PAID", OrderModel.paid_at >= since)
            .group_by(OrderItemModel.sku_id, OrderItemModel.name)
            .order_by(text("qty DESC"))
            .limit(limit)
        ).all()

    return [
        {
            "sku_id": r.sku_id,
            "name": r.name,
            "qty_sold": int(r.qty),
            "revenue": round(float(r.revenue), 2),
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 接口 3：库存预警


@router.get("/admin/inventory/alerts", response_model=None, dependencies=[Depends(_require_admin)])
async def inventory_alerts(
    low_threshold: int = Query(default=5, ge=1, le=50),
) -> dict[str, Any]:
    """售罄（stock=0）+ 紧张（1 ≤ stock ≤ low_threshold）两列。"""
    with _db() as db:
        rows = db.execute(
            select(
                ProductModel.sku_id,
                ProductModel.name,
                ProductModel.category,
                ProductModel.stock,
                ProductModel.price,
            )
            .where(ProductModel.stock <= low_threshold)
            .order_by(ProductModel.stock.asc(), ProductModel.name.asc())
        ).all()

    sold_out = [
        {
            "sku_id": r.sku_id,
            "name": r.name,
            "category": r.category,
            "stock": int(r.stock),
            "price": float(r.price),
        }
        for r in rows
        if r.stock <= 0
    ]
    low_stock = [
        {
            "sku_id": r.sku_id,
            "name": r.name,
            "category": r.category,
            "stock": int(r.stock),
            "price": float(r.price),
        }
        for r in rows
        if 1 <= r.stock <= low_threshold
    ]
    return {"sold_out": sold_out, "low_stock": low_stock, "threshold": low_threshold}


# ---------------------------------------------------------------- 接口 4：销售趋势（按天）


@router.get("/admin/sales/trend", dependencies=[Depends(_require_admin)])
async def sales_trend(
    days: int = Query(default=14, ge=1, le=60),
) -> list[dict[str, Any]]:
    """近 N 天按天聚合（销售额、订单数）。空缺天补 0。"""
    since = _day_start_local(time.time()) - (days - 1) * 86400
    with _db() as db:
        rows = db.execute(
            text(
                """
                SELECT
                  strftime('%Y-%m-%d', paid_at, 'unixepoch', 'localtime') AS d,
                  COALESCE(SUM(payable), 0) AS revenue,
                  COUNT(*) AS orders
                FROM orders
                WHERE status = 'PAID' AND paid_at >= :since
                GROUP BY d
                ORDER BY d ASC
                """
            ),
            {"since": since},
        ).all()

    bucket = {r.d: (round(float(r.revenue), 2), int(r.orders)) for r in rows}
    out: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        d = datetime.fromtimestamp(_day_start_local(time.time()) - offset * 86400)
        key = d.strftime("%Y-%m-%d")
        revenue, orders = bucket.get(key, (0.0, 0))
        out.append({"date": key, "revenue": revenue, "orders": orders})
    return out


# ---------------------------------------------------------------- 接口 5：最近对话


@router.get("/admin/chats/recent", dependencies=[Depends(_require_admin)])
async def chats_recent(
    limit: int = Query(default=10, ge=1, le=50),
) -> list[dict[str, Any]]:
    """最近 N 轮问询：把相邻 user+assistant 配成一条 session 视角的回合。"""
    with _db() as db:
        # 取最近 2*limit 条，按时间倒序，再在 Python 里配对
        rows = db.execute(
            select(
                ChatLogModel.session_id,
                ChatLogModel.role,
                ChatLogModel.content,
                ChatLogModel.citations,
                ChatLogModel.mode,
                ChatLogModel.intent,
                ChatLogModel.created_at,
            )
            .order_by(ChatLogModel.created_at.desc())
            .limit(limit * 2)
        ).all()

    # 按 user 时间倒序配对：先找最近 user 行，再往后找同 session 最近的 assistant
    turns: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        if r.role != "user":
            continue
        # assistant 是同 session 之后紧跟的一条（i+1 往往就是，跨 session 时向下找）
        assistant = None
        for j in range(i + 1, len(rows)):
            if rows[j].role == "assistant":
                assistant = rows[j]
                break
        turns.append(
            {
                "session_id": r.session_id,
                "intent": r.intent,
                "mode": r.mode,
                "question": r.content,
                "answer": assistant.content if assistant else "",
                "citations": assistant.citations if assistant else [],
                "created_at": float(r.created_at),
            }
        )
        if len(turns) >= limit:
            break
    return turns


# ---------------------------------------------------------------- 接口 6/7/8/9/10：审计追溯


def _record_summary(rec: AuditRecordModel) -> dict[str, Any]:
    return {
        "order_id": rec.order_id,
        "session_id": rec.session_id,
        "captured_at": float(rec.captured_at),
        "created_at": float(rec.created_at),
        "expires_at": float(rec.expires_at),
        "bill_payable": float(rec.bill_payable),
        "bill_origin": float(rec.bill_origin),
        "bill_discount": float(rec.bill_discount),
        "pay_method": rec.pay_method,
        "recognize_mode": rec.recognize_mode,
        "scene_id": rec.scene_id,
        "has_photo": bool(rec.photo_path),
        "items_count": len(rec.items_snapshot or []),
        "cleared": bool(rec.cleared),
        "cleared_at": float(rec.cleared_at) if rec.cleared_at else None,
        "cleared_by": rec.cleared_by,
    }


@router.get("/admin/audit", dependencies=[Depends(_require_admin)])
async def audit_list(
    days: int = Query(default=7, ge=1, le=180),
    q: str = Query(default="", description="按订单号 / 会话号 / 支付方式模糊检索"),
    cleared: str = Query(default="all", pattern="^(all|yes|no)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """审计追溯：按时间窗口 + 关键字 + 已处理状态筛选已支付订单的清单。

    不在响应里携带 base64 照片——列表卡只用缩略图，详情页单独再拉。
    """
    with _db() as db:
        rows, total = audit_service.list_records(db, days, q, cleared, limit, offset)
    return {
        "total": total,
        "window_days": days,
        "items": [_record_summary(r) for r in rows],
    }


@router.get("/admin/audit/{order_id}", dependencies=[Depends(_require_admin)])
async def audit_detail(order_id: str) -> dict[str, Any]:
    """审计追溯详情：items_snapshot + 缩略图（若有）。

    缩略图直接 inline base64 减少一次请求；原图可走单独 /photo 接口拉全分辨率。
    """
    with _db() as db:
        rec = audit_service.get_record(db, order_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        thumb_b64 = ""
        if rec.photo_path:
            settings = get_settings()
            path = audit_service.resolve_photo_path(settings, rec.photo_path)
            if path is not None:
                try:
                    import base64

                    thumb_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
                except Exception:
                    thumb_b64 = ""
        result = _record_summary(rec)
    result["items_snapshot"] = list(rec.items_snapshot or [])
    result["clear_note"] = rec.clear_note or ""
    result["photo_inline_b64"] = thumb_b64
    return result


@router.get("/admin/audit/{order_id}/photo", dependencies=[Depends(_require_admin)])
async def audit_photo(
    order_id: str,
    token: str | None = Query(default=None, description="用 ?token= 直连原图（<img src> 用），等价于 Bearer"),
) -> Any:
    """审计原图：直接返回 image/jpeg 流，<img src> 可直连。

    注：实际实现是手工构造 Response，避免 import 路径冲突。
    路径：query string 里的 token 等价于 Authorization: Bearer token，
    是给 <img src> 这种无法设 header 的场景留的入口。
    """
    from fastapi.responses import FileResponse, Response

    settings = get_settings()
    expected = (settings.ADMIN_TOKEN or "").strip()
    if expected and token != expected:
        # _require_admin 已经验过 Bearer header，但 query token 还得再验一次
        raise HTTPException(status_code=403, detail="query token 不正确")

    with _db() as db:
        rec = audit_service.get_record(db, order_id)
        if rec is None or not rec.photo_path:
            return Response(status_code=404)
        path = audit_service.resolve_photo_path(settings, rec.photo_path)
        if path is None:
            return Response(status_code=404)
    stat = path.stat()
    etag = f'W/"{int(stat.st_mtime)}-{stat.st_size}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=600",
        "X-Audit-Sha": rec.photo_sha256 or "",
    }
    return FileResponse(
        path=str(path),
        media_type="image/jpeg",
        headers=headers,
        filename=path.name,
    )


@router.post("/admin/audit/{order_id}/clear", dependencies=[Depends(_require_admin)])
async def audit_clear(order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """标记一条审计记录为已处理（防损/客诉处理完成时点用）。"""
    operator = str(payload.get("operator", "")).strip()[:64] or "unknown"
    note = str(payload.get("note", ""))[:500]
    with _db() as db:
        rec = audit_service.clear_record(db, order_id, operator, note)
        if rec is None:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        db.commit()
        summary = _record_summary(rec)
    summary["clear_note"] = rec.clear_note or ""
    return summary


@router.delete("/admin/audit/{order_id}", dependencies=[Depends(_require_admin)])
async def audit_delete(order_id: str) -> dict[str, Any]:
    """真删审计记录：删文件 + 删记录 + 写一行 chat_logs 留痕。

    误删等于丧失一份证据，所以接口名直白，文档里也明确提示「不可逆」。
    """
    settings = get_settings()
    with _db() as db:
        ok = audit_service.delete_record(db, order_id, settings)
        if not ok:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        db.commit()
    return {"deleted": True, "order_id": order_id}
