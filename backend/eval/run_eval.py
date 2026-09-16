"""导购 Agent 评测执行器：同一套用例跑三种配置，落盘原始回答。

三种配置（消融实验，A→B 加工具，B→C 加约束）：

  A  pure   纯 LLM 直答——不注册任何工具，只给店员人格。
            衡量「没有店内数据接入时，模型会编多离谱」。
  B  tools  工具可用但无边界约束——注册全部工具，system 只说身份、不给任何规则。
            衡量「光把工具挂上去够不够」。
  C  guard  工具 + 边界约束——即线上现状（app/prompts/guide.yaml 的完整 system prompt
            + 第二轮 no_more_tool_calls_note 硬约束）。
            衡量「给大模型划边界」带来的增量。

执行策略：只负责「跑 + 存」，不做判定。所有原始输入输出写进 results/raw_<arm>.jsonl，
判定交给 score_eval.py。这样改判据不用重新烧 API 额度。

用法：
    python eval/run_eval.py --arm all              # 三组全跑
    python eval/run_eval.py --arm pure --limit 3   # 冒烟
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_EVAL_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _EVAL_DIR.parent
sys.path.insert(0, str(_BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.prompts import no_more_tool_calls_note, system_prompt  # noqa: E402
from app.services import llm  # noqa: E402
from app.services.agent import build_tools, run_tool  # noqa: E402

RESULTS_DIR = _EVAL_DIR / "results"

# 温度取 0.3（两部分都用）。线上作答那一步实际是 0.4，评测刻意压到 0.3
# 以降低多次运行之间的方差——评测要的是可比性，不是逐字复现线上取词。
TEMPERATURE = 0.3
MAX_TOOL_ROUNDS = 1  # 一轮工具调用 + 一轮作答，与线上一致

# ---------------------------------------------------------------- 三组 system prompt

_ARM_A_SYSTEM = """你是「智选无人超市」的店内导购助手，通过手机小程序或店内大屏为顾客服务。
语气热情简洁，像便利店店员，单次回答控制在 150 字以内，多用短句和换行。
直接回答顾客的问题即可。"""

_ARM_B_SYSTEM = """你是「智选无人超市」的店内导购助手，通过手机小程序或店内大屏为顾客服务。
语气热情简洁，像便利店店员，单次回答控制在 150 字以内，多用短句和换行。"""


def _arm_config(arm: str) -> dict[str, Any]:
    if arm == "pure":
        return {"system": _ARM_A_SYSTEM, "tools": None, "guard_turn2": False}
    if arm == "tools":
        return {"system": _ARM_B_SYSTEM, "tools": build_tools(), "guard_turn2": False}
    if arm == "guard":
        return {"system": system_prompt(), "tools": build_tools(), "guard_turn2": True}
    raise ValueError(f"unknown arm: {arm}")


# ---------------------------------------------------------------- 单题执行


async def _ask(
    arm: str,
    case: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    cfg = _arm_config(arm)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": cfg["system"]},
        *[{"role": m["role"], "content": m["content"]} for m in case.get("history") or []],
        {"role": "user", "content": case["q"]},
    ]

    tool_trace: list[dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    started = time.perf_counter()
    error: str | None = None
    answer = ""

    try:
        for round_idx in range(MAX_TOOL_ROUNDS + 1):
            if round_idx > 0 and not tool_trace:
                # 第一轮就没调工具，没有工具结果可回灌，直接收尾
                break
            if arm != "pure" and round_idx > 0 and cfg["guard_turn2"]:
                messages.append({"role": "system", "content": no_more_tool_calls_note()})

            if round_idx == 0:
                result = await llm.chat(
                    messages, settings, tools=cfg["tools"], temperature=TEMPERATURE
                )
            else:
                # 第二轮不允许再调工具：不传 tools，从协议层面就调不了
                result = await llm.chat(messages, settings, tools=None, temperature=TEMPERATURE)

            usage = result.get("usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)

            calls = result.get("tool_calls") or []
            if calls:
                messages.append(
                    {"role": "assistant", "content": result.get("content") or None, "tool_calls": calls}
                )
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    try:
                        arguments = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    content, _cites = run_tool(name, arguments)
                    tool_trace.append(
                        {
                            "name": name,
                            "arguments": arguments,
                            "result_preview": content[:600],
                            "result_len": len(content),
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call.get("id", ""), "content": content}
                    )
                continue

            answer = result.get("content") or ""
            break
    except Exception as exc:  # 单题失败不拖垮整轮评测
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "id": case["id"],
        "cat": case["cat"],
        "arm": arm,
        "question": case["q"],
        "answer": answer,
        "tool_trace": tool_trace,
        "tool_names": [t["name"] for t in tool_trace],
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "error": error,
    }


async def _run_arm(
    arm: str,
    cases: list[dict[str, Any]],
    settings: Any,
    concurrency: int,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(cases)
    lock = asyncio.Lock()

    async def _one(case: dict[str, Any]) -> dict[str, Any]:
        nonlocal done
        async with sem:
            # 失败重试一次：网络抖动不该被记成模型答错
            record = await _ask(arm, case, settings)
            if record["error"]:
                await asyncio.sleep(1.0)
                record = await _ask(arm, case, settings)
            async with lock:
                done += 1
                mark = "ERR" if record["error"] else "ok "
                print(
                    f"[{arm}] {done:>3}/{total} {record['id']} {mark} "
                    f"{record['latency_ms']:>6}ms tools={record['tool_names']}",
                    flush=True,
                )
            return record

    return list(await asyncio.gather(*[_one(c) for c in cases]))


def main() -> int:
    parser = argparse.ArgumentParser(description="导购 Agent 三方案评测")
    parser.add_argument(
        "--arm", default="all", choices=["pure", "tools", "guard", "all"], help="跑哪一组"
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    settings = get_settings()
    if not llm.available(settings):
        print("LLM_API_KEY 未配置，无法评测。请先在 backend/.env 里填好密钥。", file=sys.stderr)
        return 2

    cases = json.loads((_EVAL_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[: args.limit]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    arms = ["pure", "tools", "guard"] if args.arm == "all" else [args.arm]

    print(
        f"模型={settings.LLM_MODEL} 用例={len(cases)} 组={arms} 并发={args.concurrency}\n",
        flush=True,
    )
    for arm in arms:
        started = time.perf_counter()
        records = asyncio.run(_run_arm(arm, cases, settings, args.concurrency))
        out = RESULTS_DIR / f"raw_{arm}.jsonl"
        with out.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        errs = sum(1 for r in records if r["error"])
        print(
            f"\n[{arm}] 完成 {len(records)} 条，失败 {errs} 条，"
            f"耗时 {time.perf_counter() - started:.1f}s → {out}\n",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
