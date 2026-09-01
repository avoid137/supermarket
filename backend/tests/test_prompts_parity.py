"""提示词抽离的逐字一致性回归测试。

这份测试的价值在于「可重复比对」：
  - 抽离过程中：拿 services 里的原文当基线，验证 YAML 一字不差
  - 抽离完成后：services 改成转发 prompts，两边本应恒等，
    但一旦有人只改了 YAML 忘了同步（或反之），这里立刻红

真要做彻底的防呆，应该把原文固化成 golden 文件。但那等于把提示词存两份，
违背抽离的初衷，所以这里只做「两条路径必须一致」的比对。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prompts import (
    no_more_tool_calls_note,
    render_vision_prompt,
    schema_hint,
    system_prompt,
    tool_prompt,
)
from app.repositories import product_repo
from app.services import agent as agent_mod
from app.services import text2sql as text2sql_mod
from app.services import vision as vision_mod
from app.services.vision import build_catalog

FAILED: list[str] = []


def check(label: str, expected: str, actual: str) -> None:
    if expected == actual:
        print(f"  [OK]   {label}")
        return
    FAILED.append(label)
    print(f"  [FAIL] {label}")
    print(f"         expected ({len(expected)} chars): {expected[:120]!r}")
    print(f"         actual   ({len(actual)} chars): {actual[:120]!r}")
    # 定位第一个不同的字符，缩进错位全靠这个查
    for i, (a, b) in enumerate(zip(expected, actual)):
        if a != b:
            print(f"         first diff at index {i}: "
                  f"expected {a!r} / actual {b!r}")
            print(f"         context: ...{expected[max(0, i - 30):i + 30]!r}")
            print(f"                  ...{actual[max(0, i - 30):i + 30]!r}")
            break
    else:
        print(f"         length differs: {len(expected)} vs {len(actual)}")


def main() -> int:
    print("=== 1. 导购主提示词 ===")
    check("SYSTEM_PROMPT", agent_mod.SYSTEM_PROMPT, system_prompt())

    print("=== 2. 防二次工具调用补丁 ===")
    check(
        "NO_MORE_TOOL_CALLS_NOTE",
        agent_mod._NO_MORE_TOOL_CALLS_NOTE,
        no_more_tool_calls_note(),
    )

    print("=== 3. SQL 表结构说明 ===")
    check("SCHEMA_HINT", text2sql_mod.SCHEMA_HINT, schema_hint())

    print("=== 4. 视觉结账提示词 ===")
    products = product_repo.list_products()
    catalog = build_catalog(products)
    print(f"  (catalog 含 {len(products)} 个 SKU)")
    check(
        "vision prompt [full]",
        vision_mod.build_vision_prompt(catalog, cropped=False),
        render_vision_prompt(catalog, cropped=False),
    )
    check(
        "vision prompt [cropped]",
        vision_mod.build_vision_prompt(catalog, cropped=True),
        render_vision_prompt(catalog, cropped=True),
    )

    print("=== 5. 工具 description ===")
    legacy = {t["function"]["name"]: t for t in agent_mod.TOOLS}
    for name in agent_mod.TOOLS:
        fname = name["function"]["name"]
        expected_desc = legacy[fname]["function"]["description"]
        actual_desc = tool_prompt(fname)["description"]
        check(f"tool desc: {fname}", expected_desc, actual_desc)

    print("=== 6. 工具参数描述 ===")
    for name in agent_mod.TOOLS:
        fname = name["function"]["name"]
        props = legacy[fname]["function"]["parameters"].get("properties", {})
        expected_map = {k: v.get("description", "") for k, v in props.items()}
        actual_map = tool_prompt(fname)["param_desc"]
        if expected_map == actual_map:
            print(f"  [OK]   param desc: {fname}")
        else:
            FAILED.append(f"param desc: {fname}")
            print(f"  [FAIL] param desc: {fname}")
            print(f"         expected: {expected_map}")
            print(f"         actual:   {actual_map}")

    print()
    if FAILED:
        print(f"=== 失败 {len(FAILED)} 项 ===")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("=== 全部逐字一致 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
