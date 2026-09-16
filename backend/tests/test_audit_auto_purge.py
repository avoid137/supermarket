"""单元测试：run_purge_loop 启动后立即清理一轮 + 后续按 interval 循环。
模拟 lifespan 的开/关场景，不经过 uvicorn。
"""

import asyncio
import sqlite3
import time
import sys
import logging
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format='[%(name)s] %(message)s')

# 用相对定位解析 backend/，避免写死本机绝对路径（换台机器就找不到）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.database import DEFAULT_DB_PATH, session_scope, get_session_factory
from app.services import audit as audit_service


def reset_table_with_expired():
    """清理 + 注入 3 条过期（expires_at=0）+ 注入 1 条正常（30 天后才到期）"""
    con = sqlite3.connect(str(DEFAULT_DB_PATH))
    con.execute('DELETE FROM audit_records')
    now = time.time()
    rows = [
        ('EXPIRED_001', 'EXPIRED_001', None, '', 0, now, '[]', 1.0, 0, 1.0, 'wechat', 'demo:S4', 'S4', 0, now, 0),
        ('EXPIRED_002', 'EXPIRED_002', None, '', 0, now, '[]', 1.0, 0, 1.0, 'wechat', 'demo:S4', 'S4', 0, now, 0),
        ('EXPIRED_003', 'EXPIRED_003', None, '', 0, now, '[]', 1.0, 0, 1.0, 'wechat', 'demo:S4', 'S4', 0, now, 0),
        ('FRESH_001',   'FRESH_001',   None, '', 0, now, '[]', 1.0, 0, 1.0, 'wechat', 'demo:S4', 'S4', 0, now, now + 30 * 86400),
    ]
    cur = con.cursor()
    cur.executemany(
        """INSERT INTO audit_records (
          order_id, session_id, photo_path, photo_sha256, photo_size_bytes,
          captured_at, items_snapshot, bill_origin, bill_discount, bill_payable,
          pay_method, recognize_mode, scene_id, cleared, created_at, expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    con.commit()
    print(f"[seed] injected 3 expired + 1 fresh → total 4")
    con.close()


def audit_count() -> int:
    con = sqlite3.connect(str(DEFAULT_DB_PATH))
    n = con.execute('SELECT count(*) FROM audit_records').fetchone()[0]
    con.close()
    return n


async def main():
    print(f"[startup] before purge: audit_count={audit_count()}")
    reset_table_with_expired()

    settings = get_settings()
    # 设置非零 interval（≈1.08 秒）：让 startup purge 必跑，下个 tick 在 ≈1.08s 后触发，
    # 整个测试 6s 能看到 startup + 至少 1 次 tick
    settings.AUDIT_PURGE_INTERVAL_HOURS = 0.0003

    stop_event = asyncio.Event()
    task = asyncio.create_task(audit_service.run_purge_loop(stop_event, settings))
    await asyncio.sleep(1)
    n_after_startup = audit_count()
    print(f"[after startup purge] audit_count={n_after_startup} (expected 1 fresh)")

    # 在 tick 过期之前，给 fresh 行设置 expires_at=0，再等 interval 触发清理
    con = sqlite3.connect(str(DEFAULT_DB_PATH))
    con.execute('UPDATE audit_records SET expires_at = 0')
    con.commit()
    con.close()
    print('[tick test] marked all 4 rows expired (including fresh)')

    await asyncio.sleep(5)  # 等下一个 interval tick（≈3.6s）
    n_after_tick = audit_count()
    print(f"[after interval tick] audit_count={n_after_tick} (expected 0)")
    print('  期望看到: [audit auto-purge: removed N expired record(s)]')

    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
    print(f"[final] audit_count={audit_count()}")


if __name__ == '__main__':
    asyncio.run(main())
