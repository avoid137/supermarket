"""受控 SQL 查询：让模型能回答预设工具覆盖不到的组合问题，同时把风险关进笼子。

五道防线：
1. 只放行 SELECT，任何写操作关键字一律拒绝
2. 表白名单，模型看不到也碰不到白名单以外的表
3. 禁止多语句（分号），掐断「SELECT ...; DROP TABLE」这类拼接
4. 强制 LIMIT，防止一次性拖走整表
5. 结果行数上限 + 单元格截断，避免超长响应拖垮对话

即便如此它仍比预设工具危险，所以只作为「高级查询」开放，
并通过工具描述明确告诉模型：能用预设工具就别用它。
"""

from __future__ import annotations

import re

from sqlalchemy import text

from app.db.database import session_scope

ALLOWED_TABLES = {
    "products",
    "promotions",
    "similar_groups",
    "checkout_sessions",
    "checkout_detections",
    "orders",
    "order_items",
}

FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "truncate", "attach", "detach", "pragma", "vacuum", "reindex",
    "begin", "commit", "rollback", "grant", "revoke",
]

MAX_ROWS = 50
MAX_CELL_LEN = 200

TABLE_PATTERN = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)

def __getattr__(name: str):
    """向后兼容转发。

    SCHEMA_HINT 已搬到 app/prompts/sql.yaml，但外部（含历史脚本）可能仍从
    本模块导入。这里用惰性转发而不是留一份模块级常量：模块级常量在 import
    时就固定了，热重载开启后改了 YAML 这边也拿不到新版本。

    PEP 562 —— 模块级 __getattr__ 只在属性查找失败时触发，
    所以不影响同名的真实定义。
    """
    if name == "SCHEMA_HINT":
        from app.prompts import schema_hint

        return schema_hint()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def validate_sql(raw_sql: str) -> tuple[bool, str]:
    """校验并归一化 SQL。返回 (是否通过, 可执行SQL 或 拒绝原因)。"""
    if not raw_sql or not raw_sql.strip():
        return False, "SQL 为空"

    sql = re.sub(r"--[^\n]*", "", raw_sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = " ".join(sql.split()).strip()

    if ";" in sql:
        return False, "不允许执行多条语句（检测到分号）"

    lowered = sql.lower()
    if not lowered.startswith("select"):
        return False, "只允许 SELECT 查询"

    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", lowered):
            return False, f"禁止使用 {keyword.upper()} 语句"

    tables = [t.lower() for t in TABLE_PATTERN.findall(sql)]
    if not tables:
        return False, "未识别到任何表名"
    unknown = sorted({t for t in tables if t not in ALLOWED_TABLES})
    if unknown:
        return False, f"不允许访问表：{', '.join(unknown)}"

    if not re.search(r"\blimit\b", lowered):
        sql = f"{sql} LIMIT {MAX_ROWS}"

    return True, sql


def run_readonly_query(raw_sql: str) -> dict:
    """执行只读查询，返回结构化结果。任何异常都变成可展示的错误信息。"""
    ok, outcome = validate_sql(raw_sql)
    if not ok:
        return {"ok": False, "error": outcome, "sql": raw_sql}

    try:
        with session_scope() as db:
            cursor = db.execute(text(outcome))
            columns = list(cursor.keys())
            rows = [list(r) for r in cursor.fetchmany(MAX_ROWS + 1)]
    except Exception as exc:
        return {"ok": False, "error": f"SQL 执行失败：{exc}", "sql": outcome}

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    clean_rows = []
    for row in rows:
        clean_rows.append([
            (str(cell)[:MAX_CELL_LEN] if cell is not None else None) for cell in row
        ])

    return {
        "ok": True,
        "columns": columns,
        "rows": clean_rows,
        "row_count": len(clean_rows),
        "truncated": truncated,
        "sql": outcome,
    }


def format_result(result: dict, max_rows: int = 20) -> str:
    """把查询结果渲染成给模型看的紧凑文本。"""
    if not result.get("ok"):
        return f"查询被拒绝：{result.get('error')}"

    columns = result["columns"]
    rows = result["rows"][:max_rows]
    if not rows:
        return "查询成功，没有符合条件的记录。"

    lines = [" | ".join(columns)]
    lines.append("-" * min(80, len(lines[0])))
    for row in rows:
        lines.append(" | ".join("" if c is None else str(c) for c in row))

    text = "\n".join(lines)
    if result.get("truncated") or len(result["rows"]) > max_rows:
        text += f"\n（共 {result['row_count']} 行，已截断显示前 {len(rows)} 行）"
    return text
