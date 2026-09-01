"""手动清理过期审计记录：

    cd backend && python -m app.cli.purge_audit

后续会用 cron 或后台定时任务接管，先把命令留好。
"""

from __future__ import annotations

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.services.audit import purge_expired


def main() -> int:
    with get_session_factory()() as db:
        n = purge_expired(db, get_settings())
    print(f"[audit] 已清理 {n} 条过期记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
