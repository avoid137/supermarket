"""视觉结账的审计追溯服务。

写入分两个时机：
1. recognize() 收到首帧时，调 ``save_pending`` 写到 ``_pending/{session_id}.jpg``。
   这步的目的是「不等支付完成就先抓住证据」，万一中途断电也能回查当时拍了啥。
2. pay() 成功时，调 ``promote_and_persist``：把 _pending 文件改名到正式目录，
   同时把不可变 items_snapshot 与时间戳落进 audit_records 表。

demo 场景（recognize_mode 以 ``demo:`` 开头）没有真实抓拍可写，
只写一条占位记录：photo_path 为空、识别模式明确标 demo，看板据此灰态显示。

写盘失败一律降级为 log.warning 不抛错。主链路是结账不是审计，审计数据缺失
不能让顾客结不了账；但要在日志里显著出来，便于后台定位。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AuditRecordModel, OrderItemModel, OrderModel
from app.models.schemas import Bill

_logger = logging.getLogger("smartmart.audit")


def _photo_root(settings: Settings) -> Path:
    raw = (settings.AUDIT_PHOTO_DIR or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # 默认 backend/data/audit_photos，与 SQLite 文件同目录便于运维
    backend = Path(__file__).resolve().parents[2]
    return (backend / "data" / "audit_photos").resolve()


def _pending_dir(settings: Settings) -> Path:
    d = _photo_root(settings) / "_pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audit_path(settings: Settings, session_id: str, captured_at: float) -> Path:
    """按月分桶：audit_photos/{YYYY-MM}/{session_id}.jpg，便于运维分批清理。"""
    bucket = datetime.fromtimestamp(captured_at).strftime("%Y-%m")
    d = _photo_root(settings) / bucket
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.jpg"


def _compress_jpeg(raw: bytes, settings: Settings) -> bytes:
    """按配置把原始字节压成 JPEG 落盘。

    raw 不一定是 JPEG：可能是 PNG（示例场景）或其他。统一靠 PIL 兜底转码，
    转不动就原样落盘——审计场景宁可保真也别白屏。
    """
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        max_dim = max(int(settings.AUDIT_MAX_DIMENSION or 0), 320)
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=int(settings.AUDIT_JPEG_QUALITY or 82))
        return out.getvalue()
    except Exception as exc:
        _logger.warning("压缩失败，按原样落盘: %s", exc)
        return raw


def _sha256_head(data: bytes, length: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:length]


def save_pending(session_id: str, image_b64: str, settings: Settings) -> bool:
    """recognize() 收到首帧时调，把图写到 _pending/{session_id}.jpg。

    返回是否真的写下了照片。失败仅记录，不抛错。
    """
    if not session_id or not image_b64:
        return False
    try:
        import base64

        raw = base64.b64decode(image_b64)
        if not raw:
            return False
        compressed = _compress_jpeg(raw, settings)
        path = _pending_dir(settings) / f"{session_id}.jpg"
        path.write_bytes(compressed)
        return True
    except Exception as exc:
        _logger.warning("临时存档失败 session=%s: %s", session_id, exc)
        return False


def _pending_path(settings: Settings, session_id: str) -> Path | None:
    p = _pending_dir(settings) / f"{session_id}.jpg"
    return p if p.exists() else None


def promote_and_persist(
    db: Session,
    settings: Settings,
    snapshot,
    bill: Bill,
    order: OrderModel,
    recognized_mode: str,
    scene_id: str | None,
    recognized_at: float,
    items_iter: Iterable[OrderItemModel],
) -> AuditRecordModel | None:
    """支付成功时调用：迁移照片 + 落 audit_records 一行。幂等。

    幂等键：order_id（unique）。重复支付时同一 order 只写一行。
    demo 场景也会调，但 photo_path 留空，明确告诉看板「这是演示数据，不是真实抓拍」。
    """
    if not order or not order.order_id:
        return None

    is_demo = recognized_mode.startswith("demo:")

    try:
        existing = db.query(AuditRecordModel).filter(
            AuditRecordModel.order_id == order.order_id
        ).one_or_none()
        if existing is not None:
            return existing  # 幂等：重试时不要再写第二行

        photo_path: str | None = None
        photo_bytes = 0
        photo_sha = ""

        if not is_demo:
            pending = _pending_path(settings, order.session_id)
            if pending is not None:
                target = _audit_path(settings, order.session_id, recognized_at)
                try:
                    shutil.move(str(pending), str(target))
                    photo_path = str(target.relative_to(_photo_root(settings)))
                    photo_bytes = target.stat().st_size
                    photo_sha = _sha256_head(target.read_bytes())
                except Exception as exc:
                    _logger.warning("迁移照片到正式目录失败: %s", exc)

        expires_at = recognized_at + max(int(settings.AUDIT_RETENTION_DAYS or 30), 1) * 86400

        items_snapshot = [
            {
                "sku_id": it.sku_id,
                "name": it.name,
                "spec": it.spec,
                "quantity": it.quantity,
                "unit_price": it.unit_price,
                "subtotal": it.subtotal,
                "source": it.source,
                "confidence": it.confidence,
                "promotions": list(it.promotions or []),
            }
            for it in items_iter
        ]

        record = AuditRecordModel(
            order_id=order.order_id,
            session_id=order.session_id,
            photo_path=photo_path,
            photo_sha256=photo_sha,
            photo_size_bytes=photo_bytes,
            captured_at=recognized_at,
            items_snapshot=items_snapshot,
            bill_origin=bill.origin_amount,
            bill_discount=bill.discount_amount,
            bill_payable=bill.payable,
            pay_method=order.pay_method,
            recognize_mode=recognized_mode,
            scene_id=scene_id,
            cleared=False,
            created_at=order.paid_at or recognized_at,
            expires_at=expires_at,
        )
        db.add(record)
        db.flush()
        return record
    except Exception as exc:
        _logger.warning("落审计记录失败 order=%s: %s", order.order_id, exc)
        return None


def list_records(
    db: Session,
    days: int,
    q: str,
    cleared: str,
    limit: int,
    offset: int,
) -> tuple[list[AuditRecordModel], int]:
    now = datetime.now().timestamp()
    since = now - max(days, 1) * 86400

    query = db.query(AuditRecordModel).filter(AuditRecordModel.created_at >= since)

    if cleared == "yes":
        query = query.filter(AuditRecordModel.cleared.is_(True))
    elif cleared == "no":
        query = query.filter(AuditRecordModel.cleared.is_(False))

    q_norm = (q or "").strip()
    if q_norm:
        like = f"%{q_norm}%"
        query = query.filter(
            AuditRecordModel.order_id.like(like)
            | AuditRecordModel.session_id.like(like)
            | AuditRecordModel.pay_method.like(like)
        )

    total = query.count()
    rows = (
        query.order_by(AuditRecordModel.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 100))
        .all()
    )
    return rows, total


def get_record(db: Session, order_id: str) -> AuditRecordModel | None:
    return (
        db.query(AuditRecordModel)
        .filter(AuditRecordModel.order_id == order_id)
        .one_or_none()
    )


def clear_record(
    db: Session, order_id: str, operator: str, note: str
) -> AuditRecordModel | None:
    rec = get_record(db, order_id)
    if rec is None:
        return None
    import time as _t

    rec.cleared = True
    rec.cleared_at = _t.time()
    rec.cleared_by = (operator or "").strip()[:64] or "unknown"
    rec.clear_note = (note or "").strip()[:500]
    db.flush()
    return rec


def delete_record(db: Session, order_id: str, settings: Settings) -> bool:
    """真删：删文件 + 删记录。删除前把痕迹写进 chat_logs 便于回溯。"""
    rec = get_record(db, order_id)
    if rec is None:
        return False

    if rec.photo_path:
        try:
            path = _photo_root(settings) / rec.photo_path
            if path.exists():
                path.unlink()
        except Exception as exc:
            _logger.warning("删照片失败: %s", exc)

    try:
        from app.db.models import ChatLogModel

        db.add(
            ChatLogModel(
                session_id=rec.session_id,
                role="assistant",
                content=f"audit:deleted order={rec.order_id}",
                citations=[],
                mode="audit",
                intent="audit_delete",
                created_at=__import__("time").time(),
            )
        )
    except Exception:
        pass

    db.delete(rec)
    db.flush()
    return True


def purge_expired(db: Session, settings: Settings, now: float | None = None) -> int:
    """清理过期的审计记录：先删文件再删记录，逐行处理保证部分失败不回滚整批。"""
    import time as _t

    now_ts = now if now is not None else _t.time()
    expired = (
        db.query(AuditRecordModel)
        .filter(AuditRecordModel.expires_at <= now_ts)
        .all()
    )
    deleted = 0
    for rec in expired:
        if rec.photo_path:
            try:
                path = _photo_root(settings) / rec.photo_path
                if path.exists():
                    path.unlink()
            except Exception:
                pass
        db.delete(rec)
        deleted += 1
    db.commit()
    return deleted


async def run_purge_loop(stop_event, settings: Settings) -> None:
    """后台循环跑清理：先跑一次清历史残留，之后每 N 小时一次，独立异常被吞掉不打扰主链路。

    这是 lifespan 启动的协程任务，stop_event.set() 时退出循环。
    不写在 daemon 线程是因为那时拿不到 asyncio event loop，
    FastAPI 的数据库会话又绑定在同步 SQLAlchemy 上，asyncio.to_thread 更顺。
    """
    import asyncio
    import logging as _lg

    interval_hours = float(settings.AUDIT_PURGE_INTERVAL_HOURS or 0)
    if not settings.AUDIT_AUTO_PURGE or interval_hours <= 0:
        _logger.info("audit auto-purge disabled (interval_hours=%s, enabled=%s)",
                     interval_hours, settings.AUDIT_AUTO_PURGE)
        return

    interval_sec = interval_hours * 3600
    _logger.info("audit auto-purge started: every %s hour(s)", interval_hours)

    # 第一轮：启动后立即清理历史残留。迁移老库 / 误改保留期 都会留下过期数据，
    # 启动那一刻最适合一次性扫掉
    try:
        from app.db.database import get_session_factory

        def _do_purge():
            with get_session_factory()() as db:
                return purge_expired(db, settings)

        loop = asyncio.get_event_loop()
        deleted = await loop.run_in_executor(None, _do_purge)
        if deleted:
            _logger.warning("audit startup purge: removed %d expired record(s)", deleted)
        else:
            _logger.info("audit startup purge: nothing to clean")
    except Exception as exc:
        _logger.warning("audit startup purge failed: %s", exc)

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            # 正常一次 tick：跑清理
            try:
                from app.db.database import get_session_factory

                def _do_purge():
                    with get_session_factory()() as db:
                        return purge_expired(db, settings)

                loop = asyncio.get_event_loop()
                deleted = await loop.run_in_executor(None, _do_purge)
                if deleted:
                    _logger.warning("audit auto-purge: removed %d expired record(s)", deleted)
            except Exception as exc:
                _logger.warning("audit auto-purge failed: %s", exc)

    _logger.info("audit auto-purge stopped")


def resolve_photo_path(settings: Settings, photo_path: str) -> Path | None:
    """把表中相对路径拼回绝对路径，并校验仍在 root 下——防 ../ 越权读盘。"""
    if not photo_path:
        return None
    root = _photo_root(settings)
    try:
        abs_path = (root / photo_path).resolve()
        abs_path.relative_to(root)  # 若被绕出会抛 ValueError
        return abs_path if abs_path.exists() else None
    except ValueError:
        return None
