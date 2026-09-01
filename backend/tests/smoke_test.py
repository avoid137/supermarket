"""端到端冒烟测试：不依赖任何 API Key，覆盖三条结账路径与导购问答。

运行：python tests/smoke_test.py（需先启动后端）
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000/api/v1"


def new_session() -> str:
    return httpx.post(f"{BASE}/checkout/sessions", timeout=10).json()["session_id"]


def show_bill(bill: dict) -> None:
    for item in bill["items"]:
        promo = f"  [{'; '.join(item['promotions'])}]" if item["promotions"] else ""
        print(f"   - {item['name']} x{item['quantity']}"
              f"  ¥{item['unit_price']} → 小计 ¥{item['subtotal']}，优惠 ¥{item['discount']}{promo}")
    print(f"   原价 ¥{bill['origin_amount']}｜优惠 ¥{bill['discount_amount']}｜应付 ¥{bill['payable']}")


def main() -> int:
    failed = 0

    health = httpx.get(f"{BASE}/health", timeout=10).json()
    print(f"[health] {health['service']}，商品 {health['product_count']} 个，"
          f"LLM={health['llm_enabled']}，Vision={health['vision_enabled']}")

    # ---- S1 顺畅路径 ----
    print("\n=== S1 日常小采购（应全部自动入账）===")
    sid = new_session()
    r = httpx.post(f"{BASE}/checkout/sessions/{sid}/recognize",
                   json={"scene_id": "S1", "tray_weight_g": 1355, "use_vision_model": False}, timeout=30).json()
    states = {d["box_id"]: d["state"] for d in r["detections"]}
    print(f"   mode={r['mode']} 状态={states}")
    print(f"   称重：{r['weight_check']['message']}")
    snap = httpx.post(f"{BASE}/checkout/sessions/{sid}/confirm?member=true", timeout=10).json()
    print(f"   state={snap['state']}")
    show_bill(snap["bill"])
    if snap["state"] != "CONFIRMED" or r["need_clarify"] or r["need_review"]:
        print("   [FAIL] S1 应无歧义且直接进入已确认"); failed += 1
    if r["weight_check"]["status"] != "ok":
        print("   [FAIL] S1 重量校验应通过"); failed += 1
    paid = httpx.post(f"{BASE}/checkout/sessions/{sid}/pay", json={"method": "wechat"}, timeout=30).json()
    print(f"   支付后 state={paid['state']}")
    if paid["state"] != "PAID":
        print("   [FAIL] 支付后应为 PAID"); failed += 1

    # ---- S2 相似包装 ----
    print("\n=== S2 相似包装（应触发追问）===")
    sid = new_session()
    r = httpx.post(f"{BASE}/checkout/sessions/{sid}/recognize",
                   json={"scene_id": "S2", "tray_weight_g": 700, "use_vision_model": False}, timeout=30).json()
    for d in r["detections"]:
        names = " / ".join(f"{c['name']}({c['score']})" for c in d["candidates"])
        print(f"   {d['box_id']} [{d['state']}] {names}")
        if d["message"]:
            print(f"      → {d['message']}")
    if not r["need_clarify"]:
        print("   [FAIL] S2 应触发 clarify"); failed += 1

    # 顾客确认第一项为白桃味（改选候选第二名）
    target = next(d for d in r["detections"] if d["state"] == "clarify")
    alt_sku = target["candidates"][1]["sku_id"]
    snap = httpx.post(f"{BASE}/checkout/sessions/{sid}/clarify",
                      json={"box_id": target["box_id"], "sku_id": alt_sku}, timeout=10).json()
    print(f"   顾客改选 {alt_sku} 后 state={snap['state']}")
    if snap["state"] == "NEED_CLARIFY":
        second = next((d for d in snap["detections"]
                       if d["state"] == "clarify" and d["box_id"] not in snap["resolved"]), None)
        if second:
            snap = httpx.post(f"{BASE}/checkout/sessions/{sid}/clarify",
                              json={"box_id": second["box_id"], "sku_id": second["candidates"][0]["sku_id"]},
                              timeout=10).json()
            print(f"   再确认一项后 state={snap['state']}")
    show_bill(snap["bill"])
    if snap["state"] != "CONFIRMED":
        print("   [FAIL] 全部澄清后应为 CONFIRMED"); failed += 1

    # ---- S3 遮挡 + 漏检 ----
    print("\n=== S3 拥挤与遮挡（应转人工 + 称重报警）===")
    sid = new_session()
    r = httpx.post(f"{BASE}/checkout/sessions/{sid}/recognize",
                   json={"scene_id": "S3", "tray_weight_g": 865, "use_vision_model": False}, timeout=30).json()
    for d in r["detections"]:
        if d["state"] != "auto":
            print(f"   {d['box_id']} [{d['state']}] {d['message']}")
    print(f"   称重：{r['weight_check']['message']}")
    if not r["need_review"]:
        print("   [FAIL] S3 应存在 review 项"); failed += 1
    if r["weight_check"]["status"] == "ok":
        print("   [FAIL] S3 重量校验应报警"); failed += 1

    # 人工复核：补入被遮挡商品后，状态转 CONFIRMED 且重量校验应转为通过
    review_box = next(d for d in r["detections"] if d["state"] == "review")
    snap = httpx.post(
        f"{BASE}/checkout/sessions/{sid}/clarify",
        json={"box_id": review_box["box_id"], "sku_id": "SKU017"},
        timeout=10,
    ).json()
    manual = [i for i in snap["bill"]["items"] if i["source"] == "manual"]
    print(f"   人工补入后 state={snap['state']}，账单 {len(snap['bill']['items'])} 行，应付 ¥{snap['bill']['payable']}")
    print(f"   称重：{snap['weight_check']['message']}")
    if snap["state"] != "CONFIRMED" or not manual:
        print("   [FAIL] 人工补入后应转为 CONFIRMED 且含 manual 来源行"); failed += 1
    if snap["weight_check"]["status"] != "ok":
        print("   [FAIL] 补入正确商品后重量校验应转为通过"); failed += 1

    # 人工复核允许指定候选以外的商品（模型连候选都给不准时的兜底）
    sid2 = new_session()
    httpx.post(
        f"{BASE}/checkout/sessions/{sid2}/recognize",
        json={"scene_id": "S3", "tray_weight_g": 865, "use_vision_model": False},
        timeout=30,
    )
    resp = httpx.post(
        f"{BASE}/checkout/sessions/{sid2}/clarify",
        json={"box_id": "S3-6", "sku_id": "SKU019"},
        timeout=10,
    )
    if resp.status_code == 200:
        print("   人工可指定候选外商品：允许")
    else:
        print(f"   [FAIL] 人工指定候选外商品应被允许，实际 {resp.status_code}"); failed += 1

    # ---- 导购问答 ----
    print("\n=== 导购问答（本地知识库流式）===")
    with httpx.stream("POST", f"{BASE}/agent/ask",
                      json={"question": "可乐在哪里", "use_llm": True}, timeout=30) as resp:
        text, meta = "", {}
        for line in resp.iter_lines():
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                if ev["type"] == "meta":
                    meta = ev
                elif ev["type"] == "citations":
                    print(f"   引用：{[c['name'] for c in ev['items']]}")
                elif ev["type"] == "delta":
                    text += ev["text"]
                elif ev["type"] == "done":
                    print(f"   mode={meta.get('mode')} intent={meta.get('intent')} 耗时 {ev['latency_ms']}ms")
        print("   回答：" + text.replace("\n", " / "))
        if "A1" not in text:
            print("   [FAIL] 位置类问答应给出货架编号"); failed += 1

    print("\n" + ("全部通过" if failed == 0 else f"存在 {failed} 项失败"))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
