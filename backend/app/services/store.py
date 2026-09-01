"""结账会话存储（SQLite 持久化）。

之前用内存字典，后端一重启账单就丢——演示到一半重启就得从头再来。
现在落到数据库，服务重启后会话、检测结果、确认记录都还在。

对外接口（create / get / save / set_detections / clear）保持不变，
服务层无需感知底下换成了数据库。
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import delete, select

from app.db.database import session_scope
from app.db.models import CheckoutSessionModel, DetectionModel
from app.models.schemas import (
    Bill,
    Candidate,
    Detection,
    SessionSnapshot,
    SessionState,
)


class SessionStore:
    def create(self) -> SessionSnapshot:
        snapshot = SessionSnapshot(
            session_id=f"CS{uuid.uuid4().hex[:12].upper()}",
            state=SessionState.CREATED,
            created_at=time.time(),
        )
        self.save(snapshot)
        return snapshot

    def get(self, session_id: str) -> SessionSnapshot | None:
        with session_scope() as db:
            row = db.get(CheckoutSessionModel, session_id)
            if row is None:
                return None
            detections = db.scalars(
                select(DetectionModel)
                .where(DetectionModel.session_id == session_id)
                .order_by(DetectionModel.id)
            ).all()
        return self._to_snapshot(row, detections)

    def save(self, snapshot: SessionSnapshot) -> None:
        with session_scope() as db:
            row = db.get(CheckoutSessionModel, snapshot.session_id)
            if row is None:
                row = CheckoutSessionModel(session_id=snapshot.session_id)
                db.add(row)

            row.state = self._state_value(snapshot.state)
            row.mode = snapshot.mode
            row.member = snapshot.member
            row.tray_weight_g = snapshot.tray_weight_g
            row.created_at = snapshot.created_at
            row.updated_at = time.time()
            row.paid_method = snapshot.paid_method
            row.resolved = dict(snapshot.resolved)
            row.weight_check = snapshot.weight_check
            row.bill = snapshot.bill.model_dump() if snapshot.bill else None

            # 检测项整体替换：数量很少（一盘货最多十几个），这样做最简单也最不容易写错
            db.execute(
                delete(DetectionModel).where(DetectionModel.session_id == snapshot.session_id)
            )
            for d in snapshot.detections:
                db.add(
                    DetectionModel(
                        session_id=snapshot.session_id,
                        box_id=d.box_id,
                        x=d.x, y=d.y, w=d.w, h=d.h,
                        quantity=d.quantity,
                        state=d.state,
                        message=d.message,
                        candidates=[c.model_dump() for c in d.candidates],
                    )
                )

    def set_detections(
        self, session_id: str, detections: list[Detection], mode: str
    ) -> SessionSnapshot:
        snapshot = self.get(session_id)
        if snapshot is None:
            raise KeyError(f"会话不存在: {session_id}")
        snapshot.detections = detections
        snapshot.mode = mode
        self.save(snapshot)
        return snapshot

    def clear(self, session_id: str) -> None:
        with session_scope() as db:
            db.execute(
                delete(DetectionModel).where(DetectionModel.session_id == session_id)
            )
            db.execute(
                delete(CheckoutSessionModel).where(
                    CheckoutSessionModel.session_id == session_id
                )
            )

    @staticmethod
    def _state_value(state) -> str:
        return state.value if hasattr(state, "value") else str(state)

    @staticmethod
    def _to_snapshot(row: CheckoutSessionModel, detections) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=row.session_id,
            state=SessionState(row.state),
            mode=row.mode or "mock",
            created_at=row.created_at or 0,
            member=bool(row.member),
            tray_weight_g=row.tray_weight_g,
            paid_method=row.paid_method,
            resolved=dict(row.resolved or {}),
            weight_check=row.weight_check,
            bill=Bill(**row.bill) if row.bill else None,
            detections=[
                Detection(
                    box_id=d.box_id,
                    x=d.x, y=d.y, w=d.w, h=d.h,
                    quantity=d.quantity,
                    state=d.state,
                    message=d.message or "",
                    candidates=[Candidate(**c) for c in (d.candidates or [])],
                )
                for d in detections
            ],
        )


session_store = SessionStore()
