"""临时构造一笔带真实照片的 audit 记录，仅用于 /photo 接口端到端验证。"""
import sqlite3
import sys
import time
import json
from pathlib import Path

# 按相对位置定位 backend/，避免写死本机绝对路径
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.db.database import DEFAULT_DB_PATH

con = sqlite3.connect(str(DEFAULT_DB_PATH))
cur = con.cursor()
cur.execute(
    """INSERT OR REPLACE INTO audit_records (
      order_id, session_id, photo_path, photo_sha256, photo_size_bytes, captured_at,
      items_snapshot, bill_origin, bill_discount, bill_payable, pay_method,
      recognize_mode, scene_id, cleared, created_at, expires_at
    ) VALUES (
      'AUDIT_PHOTO_DEMO','AUDIT_PHOTO_DEMO','2026-09/AUDIT_PHOTO_DEMO.jpg',
      'sha256demo12345', 8229, ?,
      ?, 18.5, 0.0, 18.5, 'wechat', 'vision', NULL, 0, ?, ?
    )""",
    (
        time.time(),
        json.dumps([{
            'sku_id': 'SKU026', 'name': '农夫山泉 水溶C100', 'spec': '445ml',
            'quantity': 1, 'unit_price': 5.0, 'subtotal': 5.0,
            'source': 'auto', 'confidence': 0.95, 'promotions': [],
        }]),
        time.time(),
        time.time() + 30 * 86400,
    ),
)
con.commit()
con.close()
print('seeded AUDIT_PHOTO_DEMO')
