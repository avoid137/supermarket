"""提示词统一出口。

业务代码只跟这个模块打交道，不要直接读 YAML —— 这样以后换存储
（比如改数据库或远端配置中心）只动 loader.py 一处。

这里的函数都是「现取现用」，不缓存成模块级常量：热重载开启时，
改完 YAML 下一次调用就能拿到新文案。

取文案的方式：
    from app.prompts import system_prompt, local_text

    system_prompt()                       # 导购主提示词
    local_text("stock.low", name="可乐", spec="330ml", count=3)
                                          # -> "· 可乐（330ml）：仅剩 3 件，建议尽快来"
"""

from __future__ import annotations

from string import Template
from typing import Any

from app.prompts import loader
from app.prompts.loader import (
    PromptLoadError,
    clear_cache,
    is_hot_reload,
    load,
    load_all,
    set_hot_reload,
    version_summary,
)

__all__ = [
    "PromptLoadError",
    "PROMPTS_VERSION",
    "clear_cache",
    "is_hot_reload",
    "loader",
    "local_raw",
    "local_text",
    "no_more_tool_calls_note",
    "render_vision_prompt",
    "schema_hint",
    "set_hot_reload",
    "system_prompt",
    "tool_names",
    "tool_prompt",
    "version_summary",
]


def _guide() -> dict[str, Any]:
    return load("guide.yaml")


def system_prompt() -> str:
    """导购 Agent 的主提示词。"""
    return str(_guide()["system_prompt"])


def no_more_tool_calls_note() -> str:
    """工具结果回灌后追加的硬约束，防止模型再发一轮 tool_call。"""
    return str(_guide()["no_more_tool_calls_note"])


def schema_hint() -> str:
    """兜底 SQL 工具用的表结构说明。"""
    return str(load("sql.yaml")["schema_hint"])


def tool_names() -> tuple[str, ...]:
    return loader.EXPECTED_TOOLS


def tool_prompt(name: str) -> dict[str, Any]:
    """取某个工具的文案。

    返回 {"description": ..., "param_desc": {...}}。
    query_product_database 的 description 里带 $schema_hint 占位符，
    这里已经注入好了，调用方拿到的就是可以直接发给模型的最终文案。
    """
    spec = _guide()["tools"][name]
    desc = str(spec["description"])
    if "$schema_hint" in desc:
        desc = Template(desc).substitute(schema_hint=schema_hint())
    return {
        "description": desc,
        "param_desc": dict(spec.get("param_desc") or {}),
    }


def render_vision_prompt(catalog: str, cropped: bool = False) -> str:
    """渲染视觉结账提示词。

    catalog 由 services/vision.py 的 build_catalog() 从数据库实时生成。
    用 string.Template 而不是 f-string，是为了让 JSON 示例里的花括号
    不必写成 {{ }} —— 那段示例是整个提示词里最容易改错的地方。

    注意：Template 只解析模板本身，不会二次解析替换进去的值，
    所以 catalog 里就算出现 $ 也安全。
    """
    data = load("checkout.yaml")
    scope = str(data["scope"]["cropped" if cropped else "full"])
    extra = str(data["extra"].get("cropped", "")) if cropped else ""
    return Template(str(data["template"])).substitute(
        scope=scope, catalog=catalog, extra=extra
    ).strip()


def _dig(data: Any, path: str) -> Any:
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PromptLoadError(
                f"local_fallback.yaml 里找不到 '{path}'（卡在 '{part}'）"
            )
        node = node[part]
    return node


def local_raw(path: str) -> Any:
    """取 local_fallback.yaml 里的原始值（可以是 str / list / dict）。"""
    return _dig(load("local_fallback.yaml"), path)


def local_text(path: str, **values: Any) -> str:
    """取一句话术并按 $占位符 注入。

    没占位符的文案（比如引用标签）直接 local_text("citation_reason.promotion") 即可。
    有占位符却不传值会抛 KeyError —— 这是故意的，拼错参数名应该立刻暴露。
    """
    node = _dig(load("local_fallback.yaml"), path)
    if not isinstance(node, str):
        raise PromptLoadError(f"local_fallback.yaml 的 '{path}' 不是字符串")
    if not values:
        return node
    return Template(node).substitute(values)


def _compute_version() -> str:
    try:
        parts = [f"{k.removesuffix('.yaml')}={v}" for k, v in version_summary().items()]
    except PromptLoadError:
        return "unavailable"
    return " ".join(parts)


PROMPTS_VERSION = _compute_version()
