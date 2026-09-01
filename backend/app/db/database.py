"""数据库连接与会话管理。

SQLite 默认，改 DATABASE_URL 即可切到 PostgreSQL。
仓库层统一用 session_scope() 拿连接，保证异常时回滚、用完即关。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Base

# backend/ 目录，数据库文件放在 backend/data/ 下
BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = BACKEND_DIR / "data" / "smartmart.db"

_engine = None
_session_factory: sessionmaker[Session] | None = None


def resolve_database_url() -> str:
    settings = get_settings()
    url = (settings.DATABASE_URL or "").strip()
    if not url:
        DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"
    return url


def get_engine():
    global _engine
    if _engine is None:
        url = resolve_database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db(seed_if_empty: bool = True) -> None:
    """建表，并在商品表为空时灌入种子数据。服务启动时调用一次。

    兼容老库：create_all 不会 ALTER 已有表，因此对历史 schema 已落地的字段，
    在这里做一次轻量级迁移（如 products.barcode），保证热升级不丢数据。
    """
    Base.metadata.create_all(bind=get_engine())
    _migrate_add_columns()

    if not seed_if_empty:
        return

    from sqlalchemy import func, select

    from app.db.models import ProductModel
    from app.db.seed import seed_products

    with session_scope() as db:
        count = db.scalar(select(func.count()).select_from(ProductModel)) or 0
    if count == 0:
        seed_products()
    else:
        # 已有数据时，仍要补齐代码侧新增的可选字段（如 barcode）
        _backfill_product_fields()


def _backfill_product_fields() -> None:
    """把代码侧新增的可选字段（barcode 等）补到已存在的 SKU 行里。

    仅更新代码定义里有值、DB 里为空的列，不会覆盖已有数据。
    演示用：seed 只在空表跑，老库里 barcode 全是 NULL；这里给 8 个样本 SKU 回填。
    """
    from sqlalchemy import select

    from app.data.products import PRODUCTS
    from app.db.models import ProductModel

    by_sku = {p.sku_id: p for p in PRODUCTS}
    with session_scope() as db:
        rows = db.scalars(select(ProductModel)).all()
        changed = False
        for row in rows:
            src = by_sku.get(row.sku_id)
            if src is None:
                continue
            if getattr(src, "barcode", None) and not row.barcode:
                row.barcode = src.barcode
                changed = True
        if not changed:
            return


def _migrate_add_columns() -> None:
    """一次性兼容迁移：把新加的列补到老库里。

    SQLAlchemy 的 create_all 不会 ALTER 已有表。手动检测列是否存在，
    缺失则 ALTER TABLE。SQLite 没有 IF NOT EXISTS for columns，所以要 pragma 探测。
    """
    from sqlalchemy import text

    from app.db.models import ProductModel

    engine = get_engine()
    table = ProductModel.__table__
    with engine.connect() as conn:
        # 仅 SQLite 走 pragma_table_info；其它数据库用 information_schema
        if engine.dialect.name == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(products)")).fetchall()
            }
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(engine.dialect)
                conn.execute(
                    text(f'ALTER TABLE products ADD COLUMN "{col.name}" {col_type}')
                )
            conn.commit()


def now_ts() -> float:
    return time.time()
