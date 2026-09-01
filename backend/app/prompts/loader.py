"""提示词加载器：YAML 读取 + 热重载 + 完整性自检。

两条不同的失败策略，别搞混：

  冷启动（进程首次加载）  —— 校验失败直接抛错，让服务起不来。
      提示词跟代码一起提交，损坏概率等同于代码损坏。静默降级到一份
      内嵌副本会导致「改了 YAML 却跑着旧文案」，这种不一致极难排查。
      所以宁可起不来，也要把问题指名道姓地抛出来。

  热重载（运行时检测到文件变更） —— 校验失败只记 error，继续用旧版本。
      服务已经起来了，不能因为有人改坏一行 YAML 就让整个店停摆。

热重载靠 mtime 比对。默认关闭（PROMPT_HOT_RELOAD=False），
因为生产环境没必要每次请求都 stat 文件，且 importlib 之外的
模块级状态刷新在多 worker 下行为不一致。
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

_logger = logging.getLogger("smartmart.prompts")

PROMPTS_DIR = Path(__file__).resolve().parent


class PromptLoadError(RuntimeError):
    """提示词缺失或损坏。"""


# 文件名 -> 必须存在的顶层 key。启动时逐项校验。
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "guide.yaml": ("version", "system_prompt", "no_more_tool_calls_note", "tools"),
    "checkout.yaml": ("version", "scope", "extra", "template"),
    "sql.yaml": ("version", "schema_hint"),
    "local_fallback.yaml": (
        "version",
        "citation_reason",
        "promotion",
        "not_found",
        "location",
        "allergen",
        "stock",
        "nutrition",
        "pairing",
        "price",
        "detail",
        "misc",
    ),
}

# 导购工具名清单。YAML 里缺一个或名字拼错都会在这里被拦下，
# 否则模型会拿到一个没有 description 的残缺工具，行为很诡异。
EXPECTED_TOOLS: tuple[str, ...] = (
    "search_product",
    "get_nutrition",
    "get_location",
    "get_promotions",
    "recommend_pairing",
    "check_stock",
    "query_product_database",
)

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = Lock()
_hot_reload = False


def set_hot_reload(enabled: bool) -> None:
    """开关热重载。由 main.py 按 PROMPT_HOT_RELOAD 配置在启动时调用一次。"""
    global _hot_reload
    _hot_reload = enabled
    _logger.info("prompts hot-reload %s", "enabled" if enabled else "disabled")


def is_hot_reload() -> bool:
    return _hot_reload


def _validate(name: str, data: dict[str, Any]) -> None:
    """校验单个 YAML 的结构。任何一项不满足都抛 PromptLoadError。"""
    missing = [k for k in REQUIRED_KEYS.get(name, ()) if k not in data]
    if missing:
        raise PromptLoadError(
            f"{name} 缺少必需字段 {missing}。"
            f"期望字段：{list(REQUIRED_KEYS.get(name, ()))}"
        )

    if name == "guide.yaml":
        tools = data.get("tools")
        if not isinstance(tools, dict):
            raise PromptLoadError(f"{name} 的 tools 必须是映射（工具名 -> 文案）")
        absent = [t for t in EXPECTED_TOOLS if t not in tools]
        if absent:
            raise PromptLoadError(
                f"{name} 的 tools 缺少工具 {absent}。当前有：{sorted(tools)}"
            )
        for tool_name, spec in tools.items():
            if not isinstance(spec, dict) or not spec.get("description"):
                raise PromptLoadError(f"{name} 的工具 {tool_name} 缺少 description")
            if "param_desc" not in spec:
                raise PromptLoadError(f"{name} 的工具 {tool_name} 缺少 param_desc（不需要参数就写 {{}}）")
        qpd = tools.get("query_product_database", {}).get("description", "")
        if "$schema_hint" not in qpd:
            raise PromptLoadError(
                f"{name} 的 query_product_database.description 必须保留 $schema_hint 占位符，"
                "否则模型拿不到表结构说明"
            )

    if name == "checkout.yaml":
        template = data.get("template", "")
        for holder in ("$scope", "$catalog", "$extra"):
            if holder not in template:
                raise PromptLoadError(f"{name} 的 template 必须保留 {holder} 占位符")
        for key in ("full", "cropped"):
            if key not in data.get("scope", {}):
                raise PromptLoadError(f"{name} 的 scope 缺少 {key}")
        if "cropped" not in data.get("extra", {}):
            raise PromptLoadError(f"{name} 的 extra 缺少 cropped")


def _read(name: str) -> tuple[float, dict[str, Any]]:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise PromptLoadError(f"提示词文件不存在：{path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptLoadError(f"读取 {path} 失败：{exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PromptLoadError(f"{name} 不是合法 YAML（缩进或特殊字符有误）：{exc}") from exc
    if not isinstance(data, dict):
        raise PromptLoadError(f"{name} 顶层必须是映射，实际是 {type(data).__name__}")
    return path.stat().st_mtime, data


def load(name: str) -> dict[str, Any]:
    """取一份提示词。热重载开启时按 mtime 决定是否重新读取。"""
    mtime, data = _read(name)

    with _lock:
        cached = _cache.get(name)
        if cached is not None:
            cached_mtime, cached_data = cached
            if not _hot_reload or cached_mtime == mtime:
                return cached_data

    # 首次加载：校验失败要炸，把问题暴露在启动阶段。
    # 热重载：校验失败只告警，继续用旧版本，别让改坏的文件拖垮在跑的服务。
    if name not in _cache:
        _validate(name, data)
    else:
        try:
            _validate(name, data)
        except PromptLoadError as exc:
            _logger.error("prompts: %s 校验未通过，继续使用上一版：%s", name, exc)
            return _cache[name][1]

    with _lock:
        _cache[name] = (mtime, data)
    return data


def load_all() -> dict[str, dict[str, Any]]:
    """冷启动自检：把所有提示词读一遍并校验。任何一份有问题就抛错。"""
    out: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_KEYS:
        out[name] = load(name)
    return out


def version_summary() -> dict[str, str]:
    """各提示词文件的版本号，给看板和日志用。"""
    summary: dict[str, str] = {}
    for name in REQUIRED_KEYS:
        try:
            summary[name] = str(load(name).get("version", "?"))
        except PromptLoadError as exc:
            summary[name] = f"ERROR: {exc}"
    return summary


def clear_cache() -> None:
    """清空缓存，强制下次重新读取。测试与运维手动刷新用。"""
    with _lock:
        _cache.clear()
