"""提示词自检：校验 app/prompts/*.yaml 的结构完整性。

改完提示词跑一下，不用等启动或等线上出问题：

    python -m app.cli.check_prompts

会检查这些：
  1. 每个 YAML 能解析、必需字段齐全
  2. 工具清单完整，每个工具都有 description 和 param_desc
  3. 占位符（$catalog / $scope / $extra / $schema_hint）还在
  4. 渲染一遍视觉提示词，确认模板能正常注入

退出码 0 = 全部通过，1 = 有问题。CI 里可以直接拿它当门禁。
"""

from __future__ import annotations

import sys

from app.prompts import (
    PROMPTS_VERSION,
    PromptLoadError,
    loader,
    local_text,
    render_vision_prompt,
    system_prompt,
    tool_names,
    tool_prompt,
)


def main() -> int:
    print(f"[prompts] 目录：{loader.PROMPTS_DIR}")
    print(f"[prompts] 版本：{PROMPTS_VERSION}")
    print()

    try:
        loader.load_all()
    except PromptLoadError as exc:
        print(f"[prompts] 校验失败：{exc}")
        return 1
    print("[prompts] 四个 YAML 全部通过结构校验")
    print()

    # 工具清单
    print(f"[prompts] 导购工具（{len(tool_names())} 个）：")
    for name in tool_names():
        try:
            spec = tool_prompt(name)
        except PromptLoadError as exc:
            print(f"    {name:<24} 失败：{exc}")
            return 1
        desc = spec["description"].splitlines()[0]
        params = ", ".join(spec["param_desc"]) or "（无参数）"
        print(f"    {name:<24} 参数[{params}]  {desc[:40]}")
    print()

    # 占位符注入是否真的能跑通
    try:
        for cropped in (False, True):
            rendered = render_vision_prompt("- SKU001 测试商品（330ml，罐装）", cropped=cropped)
            label = "裁剪模式" if cropped else "全图模式"
            print(f"[prompts] 视觉提示词[{label}] 渲染成功，{len(rendered)} 字符")
            if "$catalog" in rendered or "$scope" in rendered or "$extra" in rendered:
                print(f"[prompts] 有占位符没被替换！渲染结果异常")
                return 1
    except Exception as exc:  # noqa: BLE001 - 自检脚本要兜住一切
        print(f"[prompts] 视觉提示词渲染失败：{exc}")
        return 1

    try:
        sys_len = len(system_prompt())
        hint = local_text("stock.low", name="测试", spec="330ml", count=3)
        print(f"[prompts] 导购主提示词 {sys_len} 字符")
        print(f"[prompts] 本地话术样例：{hint}")
    except Exception as exc:  # noqa: BLE001
        print(f"[prompts] 导购提示词渲染失败：{exc}")
        return 1

    print()
    print("[prompts] 全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
